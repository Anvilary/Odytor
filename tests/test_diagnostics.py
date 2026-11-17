"""Tests for diagnostics, credential redaction, and truncation integrity."""

import os
from git_plus.diagnostics import (
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
    result = safe_payload(payload)
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
