"""Tests for diagnostics, credential redaction, and truncation integrity."""

import os
import pytest
from odytor.diagnostics import (
    TRUNCATION_MARKER,
    redact_text,
    safe_payload,
    safe_text,
    safe_text_tracked,
)


def test_redact_tokens_and_secrets() -> None:
    text = (
        "Here is a token: ghp_1234567890abcdef1234567890abcdef1234 and "
        "Bearer secret_bearer_token_xyz and "
        "Authorization: Bearer my_super_secret"
    )
    redacted = redact_text(text)
    assert "ghp_1234567890abcdef1234567890abcdef1234" not in redacted
    assert "secret_bearer_token_xyz" not in redacted
    assert "my_super_secret" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_control_characters() -> None:
    text = "Hello\x1b[31m Red Text\x1b[0m World\x00\x07"
    redacted = redact_text(text)
    assert redacted == "Hello Red Text World"


def test_safe_text_tracked_truncation() -> None:
    raw = "a" * 100
    res, was_cut = safe_text_tracked(raw, limit=50)
    assert was_cut is True
    assert res.startswith("a" * 50)
    assert res.endswith(TRUNCATION_MARKER)


def test_evidence_integrity_on_truncated_payload() -> None:
    payload = {
        "repo": "owner/repo",
        "diff": "line1\nline2\nline3\n" * 100,
        "returned_lines": 300,
        "complete": True,
        "diff_complete": True,
        "evidence_complete": True,
    }
    sanitized = safe_payload(payload, limit=50)
    assert sanitized["truncated"] is True
    assert sanitized["complete"] is False
    assert sanitized["diff_complete"] is False
    assert sanitized["evidence_complete"] is False
    assert "diff" in sanitized["truncated_fields"]
    assert sanitized["returned_lines"] < 300


def test_no_truncation_preserves_completeness() -> None:
    payload = {
        "repo": "owner/repo",
        "diff": "short diff",
        "returned_lines": 1,
        "complete": True,
        "diff_complete": True,
        "evidence_complete": True,
    }
    sanitized = safe_payload(payload, limit=1000)
    assert "truncated" not in sanitized
    assert sanitized["complete"] is True
    assert sanitized["diff_complete"] is True
    assert sanitized["evidence_complete"] is True


def test_entire_response_has_a_byte_bound_and_honest_flags():
    import json
    payload = {'head_sha': 'a'*40, 'conflicts': [
        {'path': f'file{i}.py', 'regions': [{'text': 'x'*8000}], 'regions_complete': True}
        for i in range(100)], 'paths_complete': True, 'regions_complete': True}
    result = safe_payload(payload, total_bytes=65536)
    assert len(json.dumps(result).encode()) <= 65536
    assert result['truncated']
    assert not result['paths_complete'] and not result['regions_complete']
    assert result['head_sha'] == 'a'*40


def test_nested_region_truncation_clears_completeness():
    payload = {'paths_complete': True, 'regions_complete': True,
               'conflicts': [{'regions_complete': True, 'regions': [{'text': 'x'*100}]}]}
    result = safe_payload(payload, limit=50)
    assert not result['regions_complete']
    assert not result['conflicts'][0]['regions_complete']


def test_default_response_budget_is_larger_and_configurable():
    import json
    from odytor import diagnostics
    assert diagnostics.DEFAULT_PAYLOAD_LIMIT == 262144
    payload = {'diff': '+ a diff line with ordinary content\n' * 9000, 'diff_complete': True, 'evidence_complete': True}
    try:
        diagnostics.set_payload_limit(diagnostics.DEFAULT_PAYLOAD_LIMIT)
        wide = safe_payload(payload)
        diagnostics.set_payload_limit(32768)
        narrow = safe_payload(payload)
    finally:
        diagnostics.set_payload_limit(diagnostics.DEFAULT_PAYLOAD_LIMIT)
    assert 100000 < len(json.dumps(wide)) <= 262144 and 16000 < len(json.dumps(narrow)) <= 32768
    assert wide['diff_complete'] is False and wide['truncated_fields'] == ['diff']  # still honest when it must cut
    for bad in (100, 10 ** 9, '65536', True):
        with pytest.raises(ValueError):
            diagnostics.set_payload_limit(bad)


def test_a_diff_is_not_held_to_the_small_per_string_cap_but_other_text_is():
    text = 'x' * 50000
    result = safe_payload({'diff': text, 'note': text, 'diff_complete': True})
    assert len(result['diff']) == 50000 and len(result['note']) < 9000
    assert result['truncated_fields'] == ['note'] and result['diff_complete'] is False
    explicit = safe_payload({'diff': text, 'diff_complete': True}, limit=1000)
    assert len(explicit['diff']) < 1100  # an explicit limit still binds the diff


def test_content_and_patch_and_chunk_are_exempt_from_small_per_string_cap():
    big = 'a' * 50000
    result = safe_payload({'content': big, 'patch': big, 'chunk': big, 'log': big, 'note': big})
    assert len(result['content']) == 50000
    assert len(result['patch']) == 50000
    assert len(result['chunk']) == 50000
    assert len(result['log']) == 50000
    assert len(result['note']) < 9000
    assert result['truncated_fields'] == ['note']


def test_an_over_budget_field_keeps_most_of_the_budget_instead_of_half():
    import json
    line = '+                if count == 0 or force or time.monotonic() >= deadline: unicode é ü'
    payload = {'diff': '\n'.join(line for _ in range(5000)), 'diff_complete': True, 'diff_returned_lines': 5000}
    result = safe_payload(payload, total_bytes=262144)
    size = len(json.dumps(result, ensure_ascii=True).encode())
    assert 0.9 * 262144 <= size <= 262144
    assert result['diff_complete'] is False and result['truncated_fields'] == ['diff']
    assert result['diff_returned_lines'] == len(result['diff'].removesuffix('\n[output truncated]').splitlines())


def test_incomplete_log_window_preserves_complete_structured_run_outcomes():
    view = {'structured_outcomes_complete': True, 'evidence_complete': True, 'log_complete': False,
            'tests': [{'id': 'test_ok'}],
            'log': {'tail': 'sample', 'truncated': True, 'retained_complete': True}}
    sanitized = safe_payload(view)
    assert sanitized['truncated_fields'] == ['log']
    assert sanitized['structured_outcomes_complete'] is True and sanitized['evidence_complete'] is True
    assert sanitized['log_complete'] is False and sanitized['log']['retained_complete'] is True
    view['tests'][0]['id'] = 'x' * 100
    assert safe_payload(view, limit=40)['structured_outcomes_complete'] is False
