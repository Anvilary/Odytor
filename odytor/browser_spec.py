"""Browser scenario schema, shared by Odytor and the runner (standard library only).

A scenario is data: a small ordered list of typed steps against an application served inside the sandbox.
There is no unrestricted JavaScript evaluation, no arbitrary URL, no file upload and no download: every
navigation is a path on the one local origin the runner starts, and everything is bounded. Both sides validate
independently, so a compromised control plane message or a hand-written frame still cannot widen what runs.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_STEPS = 60
MAX_SESSION_STEPS = 150
MAX_MOCKS = 20
MAX_MOCK_BODY = 65536
MAX_MOCK_TOTAL = 262144
MAX_SELECTOR = 300
MAX_TEXT = 2000
MAX_SCREENSHOTS = 8
VIEWPORT_LIMITS = (200, 3000)
PORT_RANGE = (1024, 65535)

_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]{0,499}")
_KEY = re.compile(r"(?:(?:Control|Shift|Alt|Meta)\+)*(?:[A-Za-z0-9]|F[1-9][0-9]?|Enter|Escape|Tab|Backspace|Delete|Space|Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown)")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_LOCALE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}")
_TIMEZONE = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_COLOR_SCHEMES = frozenset({"light", "dark"})
_SERVE_KINDS = ("static", "command")
_COMPARE = frozenset({"eq", "le", "ge"})


class SpecError(ValueError):
    """The scenario violates the schema; the message names the step and the rule."""


def _fail(where: str, message: str):
    raise SpecError(f"{where}: {message}")


def _int(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(where, f"must be an integer from {low} to {high}.")
    return value


def _text(value: Any, where: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit:
        _fail(where, f"must be a string of at most {limit} characters.")
    if any(ord(c) < 32 and c not in "\t\n" or ord(c) == 127 for c in value):
        _fail(where, "must not contain control characters.")
    return value


def _selector(value: Any, where: str) -> str:
    text = _text(value, where, MAX_SELECTOR)
    if "\n" in text or "\t" in text:
        _fail(where, "must be a single-line selector.")
    return text


def _path(value: Any, where: str) -> str:
    if not isinstance(value, str) or _PATH.fullmatch(value) is None or value.startswith("//") or "\\" in value:
        _fail(where, "must be an absolute path on the local application, like '/index.html' (no scheme, host or '//').")
    return value


def validate_step(step: Any, index: int) -> dict[str, Any]:
    where = f"steps[{index}]"
    if not isinstance(step, dict) or not isinstance(step.get("op"), str):
        _fail(where, "each step is an object with an 'op'.")
    op = step["op"]
    allowed: dict[str, tuple[str, ...]] = {
        "goto": ("path",), "click": ("selector",), "fill": ("selector", "text"), "type": ("selector", "text"),
        "press": ("key",), "select": ("selector", "value"), "check": ("selector",), "uncheck": ("selector",),
        "hover": ("selector",), "wait_for": (), "wait_ms": ("ms",), "screenshot": ("name",), "snapshot": (),
        "assert_visible": ("selector",), "assert_hidden": ("selector",), "assert_text": ("selector", "text"),
        "assert_count": ("selector", "count"), "assert_requests": ("path", "count"), "reset_network_counts": ()}
    optional = {"press": ("selector",), "wait_for": ("selector", "text", "timeout_ms"), "screenshot": ("full_page", "selector", "format"),
                "snapshot": ("selector",), "assert_count": ("compare",), "assert_requests": ("compare", "method"),
                "click": ("timeout_ms",), "fill": ("timeout_ms",), "type": ("timeout_ms", "delay_ms"), "select": ("timeout_ms",),
                "goto": ("wait_until",), "assert_visible": ("timeout_ms",), "assert_hidden": ("timeout_ms",), "assert_text": ("timeout_ms",)}
    if op not in allowed:
        _fail(where, f"unknown op '{op}'. Allowed: {', '.join(sorted(allowed))}.")
    extra = set(step) - {"op", *allowed[op], *optional.get(op, ())}
    if extra:
        _fail(where, f"unexpected field(s) for '{op}': {', '.join(sorted(extra))}.")
    for field in allowed[op]:
        if field not in step:
            _fail(where, f"'{op}' requires '{field}'.")
    out: dict[str, Any] = {"op": op}
    if "path" in step:
        out["path"] = _path(step["path"], f"{where}.path")
    if "selector" in step:
        out["selector"] = _selector(step["selector"], f"{where}.selector")
    if "text" in step:
        out["text"] = _text(step["text"], f"{where}.text", MAX_TEXT, allow_empty=op in {"fill", "assert_text"})
    if "value" in step:
        out["value"] = _text(step["value"], f"{where}.value", 200)
    if "key" in step:
        if not isinstance(step["key"], str) or _KEY.fullmatch(step["key"]) is None:
            _fail(f"{where}.key", "must be a key name like 'Enter', 'Tab' or 'Control+a'.")
        out["key"] = step["key"]
    if "ms" in step:
        out["ms"] = _int(step["ms"], f"{where}.ms", 0, 5000)
    if "timeout_ms" in step:
        out["timeout_ms"] = _int(step["timeout_ms"], f"{where}.timeout_ms", 100, 15000)
    if "delay_ms" in step:
        out["delay_ms"] = _int(step["delay_ms"], f"{where}.delay_ms", 0, 200)
    if "count" in step:
        out["count"] = _int(step["count"], f"{where}.count", 0, 100000)
    if "compare" in step:
        if step["compare"] not in _COMPARE:
            _fail(f"{where}.compare", "must be 'eq', 'le' or 'ge'.")
        out["compare"] = step["compare"]
    if "method" in step:
        if step["method"] not in _METHODS:
            _fail(f"{where}.method", f"must be one of {', '.join(sorted(_METHODS))}.")
        out["method"] = step["method"]
    if "wait_until" in step:
        if step["wait_until"] not in {"load", "domcontentloaded", "networkidle"}:
            _fail(f"{where}.wait_until", "must be load, domcontentloaded or networkidle.")
        out["wait_until"] = step["wait_until"]
    if "full_page" in step:
        if type(step["full_page"]) is not bool:
            _fail(f"{where}.full_page", "must be true or false.")
        out["full_page"] = step["full_page"]
    if "format" in step:
        if step["format"] not in {"png", "jpeg"}:
            _fail(f"{where}.format", "must be png or jpeg.")
        out["format"] = step["format"]
    if op == "screenshot":
        if _NAME.fullmatch(step["name"]) is None:
            _fail(f"{where}.name", "must be 1-40 characters: letters, digits, '.', '_' or '-'.")
        out["name"] = step["name"]
    if op == "wait_for":
        if not ("selector" in step) ^ ("text" in step):
            _fail(where, "'wait_for' needs exactly one of 'selector' or 'text'.")
    return out


def validate_mock(mock: Any, index: int) -> dict[str, Any]:
    where = f"mocks[{index}]"
    if not isinstance(mock, dict):
        _fail(where, "must be an object.")
    extra = set(mock) - {"method", "path", "status", "json", "body", "content_type", "delay_ms"}
    if extra:
        _fail(where, f"unexpected field(s): {', '.join(sorted(extra))}.")
    method = mock.get("method", "GET")
    if method not in _METHODS:
        _fail(f"{where}.method", f"must be one of {', '.join(sorted(_METHODS))}.")
    path = mock.get("path")
    if not isinstance(path, str) or _PATH.fullmatch(path) is None or path.startswith("//"):
        _fail(f"{where}.path", "must be an absolute path; it matches the request path, ignoring the query string.")
    if ("json" in mock) == ("body" in mock):
        _fail(where, "give exactly one of 'json' or 'body'.")
    out: dict[str, Any] = {"method": method, "path": path.split("?", 1)[0], "status": _int(mock.get("status", 200), f"{where}.status", 100, 599),
                           "delay_ms": _int(mock.get("delay_ms", 0), f"{where}.delay_ms", 0, 5000)}
    if "json" in mock:
        try:
            encoded = json.dumps(mock["json"], separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            _fail(f"{where}.json", "must be JSON-serializable.")
        out["json"], size = mock["json"], len(encoded.encode())
        out["content_type"] = "application/json"
    else:
        out["body"], size = _text(mock["body"], f"{where}.body", MAX_MOCK_BODY, allow_empty=True), len(mock["body"].encode())
        out["content_type"] = _text(mock.get("content_type", "text/plain"), f"{where}.content_type", 80)
        if re.fullmatch(r"[a-z]+/[A-Za-z0-9.+-]+", out["content_type"]) is None:
            _fail(f"{where}.content_type", "must be a media type like text/plain.")
    if size > MAX_MOCK_BODY:
        _fail(where, f"body exceeds {MAX_MOCK_BODY} bytes.")
    out["_size"] = size
    return out


def validate_serve(serve: Any, *, allowed_kinds: tuple[str, ...] = _SERVE_KINDS) -> dict[str, Any]:
    """The operator's serve policy (static directory or an approved command); never taken from a caller."""
    if not isinstance(serve, dict) or serve.get("kind") not in allowed_kinds:
        _fail("serve", f"kind must be one of: {', '.join(allowed_kinds)}.")
    out: dict[str, Any] = {"kind": serve["kind"], "port": _int(serve.get("port", 8765), "serve.port", *PORT_RANGE),
                           "ready_path": _path(serve.get("ready_path", "/"), "serve.ready_path"),
                           "ready_timeout": _int(serve.get("ready_timeout", 30), "serve.ready_timeout", 1, 120)}
    if serve["kind"] == "static":
        root = serve.get("root", ".")
        if (not isinstance(root, str) or not root or len(root) > 200 or root.startswith("/") or "\\" in root
                or any(part in {"", "..", ".git"} or part.startswith(".git") for part in root.split("/") if part != ".")):
            _fail("serve.root", "must be a plain relative directory such as '.' or 'static'.")
        out["root"] = root
    else:
        argv = serve.get("argv")
        if (not isinstance(argv, list) or not argv or len(argv) > 40
                or not all(isinstance(a, str) and a and len(a) <= 300 and "\0" not in a for a in argv)):
            _fail("serve.argv", "must be a non-empty list of strings.")
        out["argv"] = argv
    return out


def validate_scenario(scenario: Any, *, max_steps: int = MAX_STEPS) -> dict[str, Any]:
    """Normalize and bound a caller scenario: steps, mocks, viewport, locale, timezone."""
    if not isinstance(scenario, dict):
        _fail("scenario", "must be an object.")
    extra = set(scenario) - {"steps", "mocks", "viewport", "locale", "timezone", "color_scheme", "trace", "replay"}
    if extra:
        _fail("scenario", f"unexpected field(s): {', '.join(sorted(extra))}.")
    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > max_steps:
        _fail("steps", f"must be a list of 1 to {max_steps} steps.")
    normalized = [validate_step(step, i) for i, step in enumerate(steps)]
    if sum(1 for s in normalized if s["op"] == "screenshot") > MAX_SCREENSHOTS:
        _fail("steps", f"at most {MAX_SCREENSHOTS} screenshots per run.")
    names = [s["name"] for s in normalized if s["op"] == "screenshot"]
    if len(set(names)) != len(names):
        _fail("steps", "screenshot names must be unique within a run.")
    mocks_in = scenario.get("mocks", [])
    if not isinstance(mocks_in, list) or len(mocks_in) > MAX_MOCKS:
        _fail("mocks", f"must be a list of at most {MAX_MOCKS} mocks.")
    mocks = [validate_mock(m, i) for i, m in enumerate(mocks_in)]
    if sum(m["_size"] for m in mocks) > MAX_MOCK_TOTAL:
        _fail("mocks", f"total mock bodies exceed {MAX_MOCK_TOTAL} bytes.")
    for m in mocks:
        m.pop("_size")
    view = scenario.get("viewport", {"width": 1280, "height": 720})
    if not isinstance(view, dict) or set(view) - {"width", "height"}:
        _fail("viewport", "must be {width, height}.")
    viewport = {"width": _int(view.get("width", 1280), "viewport.width", *VIEWPORT_LIMITS),
                "height": _int(view.get("height", 720), "viewport.height", *VIEWPORT_LIMITS)}
    locale, timezone = scenario.get("locale", "en-US"), scenario.get("timezone", "UTC")
    if not isinstance(locale, str) or _LOCALE.fullmatch(locale) is None:
        _fail("locale", "must be a locale like 'en-US'.")
    if not isinstance(timezone, str) or _TIMEZONE.fullmatch(timezone) is None:
        _fail("timezone", "must be an IANA timezone like 'UTC' or 'Europe/Lisbon'.")
    scheme = scenario.get("color_scheme", "light")
    if scheme not in _COLOR_SCHEMES:
        _fail("color_scheme", "must be 'light' or 'dark'.")
    trace = scenario.get("trace", False)
    if type(trace) is not bool:
        _fail("trace", "must be true or false.")
    out = {"steps": normalized, "mocks": mocks, "viewport": viewport, "locale": locale, "timezone": timezone,
           "color_scheme": scheme, "trace": trace}
    if "replay" in scenario:  # steps[:replay] rebuild session state; their screenshots and evidence are not captured
        out["replay"] = _int(scenario["replay"], "replay", 0, len(normalized) - 1)
    return out
