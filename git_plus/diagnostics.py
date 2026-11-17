"""Bounded, credential-redacted diagnostics and truncation integrity."""

from __future__ import annotations

import json
import os
import re
from typing import Any

TRUNCATION_MARKER = "\n[output truncated]"
DEFAULT_TEXT_LIMIT = 8192
DEFAULT_PAYLOAD_LIMIT = 65536
MAX_RECURSION_DEPTH = 64

_CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKEN_RE = re.compile(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+|\bBearer\s+[^\s,;]+", re.I)
_HEADER_RE = re.compile(r"(?im)\b(authorization|proxy-authorization|set-cookie|cookie)\s*:[^\r\n]*")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)"
)
_URL_USER_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,32}?://)[^\s/@]+@")
_URL_QUERY_RE = re.compile(r"https?://[^\s#]+", re.I)
_SECRET_RE = re.compile(
    r"(?i)(?<![\w-])(?=[\w-]*(?:token|secret|password|credential|api[_-]?key))"
    r"((?:[\w-]+)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&;,]+)"
)


def redact_text(value: str) -> str:
    """Normalize Unicode, then redact sensitive credentials and control characters."""
    text = value.encode("utf-8", errors="replace").decode("utf-8")
    for key, secret in os.environ.items():
        if len(secret) >= 4 and any(
            marker in key.upper()
            for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "API_KEY")
        ):
            text = text.replace(secret, "[REDACTED]" + "\n" * secret.count("\n"))
    text = _CONTROL_RE.sub("", text)
    text = _PRIVATE_KEY_RE.sub(
        lambda match: "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n"),
        text,
    )
    text = _HEADER_RE.sub(lambda m: m.group(1) + ": [REDACTED]", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    text = _URL_USER_RE.sub(r"\1[REDACTED]@", text)
    text = _URL_QUERY_RE.sub(
        lambda m: m.group(0).partition("?")[0] + "?[REDACTED]" if "?" in m.group(0) else m.group(0),
        text,
    )
    text = _SECRET_RE.sub(r"\1[REDACTED]", text)
    return text


def safe_text_tracked(
    value: str,
    limit: int | None = DEFAULT_TEXT_LIMIT,
    *,
    max_bytes: int | None = None,
) -> tuple[str, bool]:
    """Redact secrets and truncate with explicit truncation tracking.

    Returns a tuple of (sanitized_string, was_truncated).
    Ensures multi-byte UTF-8 boundaries are preserved and appends TRUNCATION_MARKER
    when truncation occurs.
    """
    text = redact_text(value)
    char_cut = limit is not None and len(text) > limit

    raw_bytes = text.encode("utf-8")
    byte_cut = max_bytes is not None and len(raw_bytes) > max_bytes

    if not char_cut and not byte_cut:
        return text, False

    marker = TRUNCATION_MARKER
    marker_bytes = len(marker.encode("utf-8"))

    if char_cut and limit is not None:
        text = text[:limit]
        raw_bytes = text.encode("utf-8")

    if max_bytes is not None:
        target_bytes = max(0, max_bytes - marker_bytes)
        if len(raw_bytes) > target_bytes:
            raw_bytes = raw_bytes[:target_bytes]
            text = raw_bytes.decode("utf-8", errors="ignore")

    return text + marker, True


def safe_text(
    value: str,
    limit: int | None = DEFAULT_TEXT_LIMIT,
    *,
    max_bytes: int | None = None,
) -> str:
    """Convenience wrapper returning only the sanitized string."""
    sanitized, _ = safe_text_tracked(value, limit=limit, max_bytes=max_bytes)
    return sanitized


def safe_payload(
    payload: Any,
    limit: int | None = DEFAULT_TEXT_LIMIT,
    *,
    max_bytes: int | None = None,
    total_bytes: int = DEFAULT_PAYLOAD_LIMIT,
) -> Any:
    """Enforce credential redaction and truncation tracking across arbitrary nested structures.

    Invariant: Whenever content is truncated, set diff_complete=False, complete=False,
    and evidence_complete=False. Populate truncated_fields with exact field paths.
    """
    if total_bytes < 512:
        raise ValueError("total_bytes must be at least 512.")
    result, _, _ = _safe_payload_track(
        payload,
        "",
        limit=limit,
        max_bytes=max_bytes,
        seen=set(),
        depth=0,
    )
    if isinstance(result, dict):
        # Per-string limits alone do not bound a response containing many records.
        cuts = list(result.get("truncated_fields", []))
        while len(json.dumps(result, ensure_ascii=True).encode()) > total_bytes:
            candidates = [(len(json.dumps(v, ensure_ascii=True)), k) for k, v in result.items()
                          if isinstance(v, (str, list, dict)) and (len(v) > 1 if isinstance(v, str) else bool(v)) and k != "truncated_fields"]
            if not candidates:
                return {"error": "Response metadata exceeds the byte limit.", "evidence_complete": False,
                        "truncated": True, "truncated_fields": ["<root>"]}
            _, key = max(candidates)
            value = result[key]
            if isinstance(value, str):
                # Remove an old marker before cutting again.
                value = value.removesuffix(TRUNCATION_MARKER)
                result[key] = value[:len(value)//2] + TRUNCATION_MARKER if len(value) > 1 else ""
            elif isinstance(value, list):
                result[key] = value[:len(value)//2]
            else:
                result[key] = dict(list(value.items())[:len(value)//2])
            cuts.append(key)
            result["truncated"] = True
            result["truncated_fields"] = sorted(set(cuts))
            _mark_incomplete(result)
        if result.get("truncated") and isinstance(result.get("diff"), str):
            content = result["diff"].removesuffix(TRUNCATION_MARKER)
            if result["diff"].endswith(TRUNCATION_MARKER):
                content = content.rsplit("\n", 1)[0] if "\n" in content else ""
                result["diff"] = content + TRUNCATION_MARKER
            for count_key in ("returned_lines", "diff_returned_lines"):
                if isinstance(result.get(count_key), int):
                    result[count_key] = min(result[count_key], len(content.splitlines()))
    return result


def _mark_incomplete(value: Any) -> None:
    if isinstance(value, dict):
        for key in value:
            if key == "complete" or key.endswith("_complete"):
                value[key] = False
            else:
                _mark_incomplete(value[key])
    elif isinstance(value, list):
        for item in value:
            _mark_incomplete(item)


def _safe_payload_track(
    value: Any,
    key_path: str,
    *,
    limit: int | None,
    max_bytes: int | None,
    seen: set[int],
    depth: int,
) -> tuple[Any, list[str], bool]:
    """Internal recursive payload sanitizer tracking key paths and truncation."""
    if depth > MAX_RECURSION_DEPTH:
        path = key_path if key_path else "<root>"
        return "[recursion limit exceeded]", [path], True

    if value is None or isinstance(value, (int, float, bool)):
        return value, [], False

    if id(value) in seen:
        path = key_path if key_path else "<root>"
        return "[circular reference]", [path], True

    if isinstance(value, str):
        sanitized, was_cut = safe_text_tracked(value, limit=limit, max_bytes=max_bytes)
        paths = [key_path] if (was_cut and key_path) else ([] if not was_cut else ["<root>"])
        return sanitized, paths, was_cut

    if isinstance(value, (bytes, bytearray)):
        decoded = value.decode("utf-8", errors="replace")
        sanitized, was_cut = safe_text_tracked(decoded, limit=limit, max_bytes=max_bytes)
        paths = [key_path] if (was_cut and key_path) else ([] if not was_cut else ["<root>"])
        return sanitized, paths, was_cut

    if isinstance(value, (set, frozenset)):
        try:
            items_list = sorted(value)
        except TypeError:
            items_list = list(value)
        return _safe_payload_track(
            items_list,
            key_path,
            limit=limit,
            max_bytes=max_bytes,
            seen=seen,
            depth=depth,
        )

    if isinstance(value, dict):
        seen.add(id(value))
        try:
            result_dict: dict[str, Any] = {}
            all_truncated: list[str] = []
            diff_was_cut = False
            evidence_incomplete = False

            for k, v in value.items():
                child_key, key_cut = safe_text_tracked(str(k), limit=limit, max_bytes=max_bytes)
                base_key = child_key
                collision = 1
                while child_key in result_dict:
                    collision += 1
                    child_key = f"{base_key} [duplicate {collision}]"
                child_path = f"{key_path}.{child_key}" if key_path else child_key
                child_val, child_cuts, child_incomplete = _safe_payload_track(
                    v,
                    child_path,
                    limit=limit,
                    max_bytes=max_bytes,
                    seen=seen,
                    depth=depth + 1,
                )
                if key_cut or collision > 1:
                    child_cuts.append(child_path)
                if k == "diff" and child_cuts and isinstance(child_val, str) and child_val.endswith(TRUNCATION_MARKER):
                    diff_was_cut = True
                    content = child_val[: -len(TRUNCATION_MARKER)]
                    last_nl = content.rfind("\n")
                    child_val = (content[:last_nl] if last_nl >= 0 else "") + TRUNCATION_MARKER

                result_dict[child_key] = child_val
                all_truncated.extend(child_cuts)
                evidence_incomplete = evidence_incomplete or child_incomplete

            if result_dict.get("truncated") is True:
                existing = result_dict.get("truncated_fields")
                fields = [p for p in existing if isinstance(p, str)] if isinstance(existing, list) else []
                all_truncated.extend(f"{key_path}.{p}" if key_path else p for p in fields)
                if not fields and not all_truncated:
                    all_truncated.append(key_path or "<root>")

            if all_truncated:
                result_dict["truncated"] = True
                _mark_incomplete(result_dict)

                if diff_was_cut and "returned_lines" in result_dict and isinstance(result_dict["returned_lines"], int):
                    diff_val = result_dict.get("diff")
                    if isinstance(diff_val, str):
                        actual_lines = len(diff_val.removesuffix(TRUNCATION_MARKER).splitlines())
                        if actual_lines < result_dict["returned_lines"]:
                            result_dict["returned_lines"] = actual_lines

                if not key_path:
                    existing = result_dict.get("truncated_fields")
                    if isinstance(existing, list) and all(isinstance(p, str) for p in existing):
                        result_dict["truncated_fields"] = sorted(list(set(existing + all_truncated)))
                    else:
                        result_dict["truncated_fields"] = sorted(set(all_truncated))

            return result_dict, all_truncated, evidence_incomplete or bool(all_truncated)
        finally:
            seen.remove(id(value))

    if isinstance(value, (list, tuple)):
        seen.add(id(value))
        try:
            result_list: list[Any] = []
            all_truncated = []
            evidence_incomplete = False
            for idx, item in enumerate(value):
                child_path = f"{key_path}[{idx}]"
                child_val, child_cuts, child_incomplete = _safe_payload_track(
                    item,
                    child_path,
                    limit=limit,
                    max_bytes=max_bytes,
                    seen=seen,
                    depth=depth + 1,
                )
                result_list.append(child_val)
                all_truncated.extend(child_cuts)
                evidence_incomplete = evidence_incomplete or child_incomplete
            return result_list, all_truncated, evidence_incomplete
        finally:
            seen.remove(id(value))

    str_repr, was_cut = safe_text_tracked(repr(value), limit=limit, max_bytes=max_bytes)
    paths = [key_path] if (was_cut and key_path) else ([] if not was_cut else ["<root>"])
    return str_repr, paths, was_cut
