"""Reviewer test overlays: a bounded, test-only unified diff applied to a disposable tree.

This is deliberately separate from quick_fix's patch policy. quick_fix pushes to a contributor
branch, so it forbids new files and security-sounding names; an overlay never leaves the
disposable tree, so it must allow new test files (including ones about authentication) but
only inside maintainer-configured test directories. Nothing here is a sandbox: overlay code
is untrusted and runs only inside the isolated runner.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from odytor.diagnostics import safe_text
from odytor.errors import GitOperationError, SecurityError, ValidationError
from odytor.git_workspace import GitWorkspace
from odytor.quick_fix import _HUNK_HEADER_RE

MAX_FILES = 20
MAX_CHANGED_LINES = 2000
MAX_RESULT_FILE_BYTES = 512 * 1024
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+@-]*")
_ALLOWED_EXTENSIONS = frozenset({".py", ".txt", ".json", ".yaml", ".yml", ".csv", ".md", ".toml"})
_FORBIDDEN_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "noxfile.py", "pipfile", "pipfile.lock",
    "poetry.lock", "uv.lock", "sitecustomize.py", "usercustomize.py", "makefile", "dockerfile", "compose.yaml",
    "compose.yml", "docker-compose.yml", "docker-compose.yaml", "package.json", "package-lock.json"})
_TEST_FILE = re.compile(r"(?:test_.+|.+_test)\.py")


@dataclass(frozen=True)
class OverlaySummary:
    files: tuple[str, ...]
    new_files: tuple[str, ...]
    added_lines: int
    removed_lines: int
    sha256: str
    patch_bytes: int
    canonical: str = ""  # the exact text that was validated, hashed and is applied (trailing blank lines stripped)

    @property
    def test_files(self) -> tuple[str, ...]:
        """Overlay files pytest would collect; the result must show these actually ran."""
        return tuple(p for p in self.files if _TEST_FILE.fullmatch(p.rsplit("/", 1)[-1]))


_EXCERPT_LINE = 160
_FILE_HEADER = re.compile(r"diff --git a/([^\s\"\\]+) b/([^\s\"\\]+)")
_SECTION_START = ("diff --git ", "@@")


def _excerpt(lines: list[str], index: int, radius: int = 2) -> list[dict[str, Any]]:
    """A few numbered lines around the failure; each line is clipped so diagnostics stay small."""
    low, high = max(index - radius, 0), min(index + radius + 1, len(lines))
    return [{"line": n + 1, "text": safe_text(lines[n][:_EXCERPT_LINE], _EXCERPT_LINE + 16), "failing": n == index}
            for n in range(low, high)]


def _reject(kind: type[Exception], stage: str, message: str, lines: list[str], index: int, **extra: Any):
    """Raise with the failing stage and 1-based line so a caller can repair the patch rather than guess."""
    index = min(max(index, 0), max(len(lines) - 1, 0))
    detail = {"stage": stage, "line": index + 1 if lines else 0, "excerpt": _excerpt(lines, index) if lines else [], **extra}
    raise kind(f"Overlay rejected at line {detail['line']} ({stage}): {message}", detail)


def validate_overlay_path(path: str, test_dirs: tuple[str, ...]) -> str:
    parts = path.split("/")
    if (not path or len(path) > 300 or any(_COMPONENT.fullmatch(p) is None or p in {".", ".."} for p in parts)
            or any(p.lower().startswith(".git") for p in parts)):
        raise SecurityError(f"Overlay path is not a plain repository-relative path: {path[:80]!r}.")
    if not any(path.startswith(root + "/") for root in test_dirs):
        raise SecurityError(f"Overlay path is outside the configured test directories: {path}.")
    name = parts[-1].lower()
    if name in _FORBIDDEN_NAMES or name.startswith("requirements") or name.endswith(".pth"):
        raise SecurityError(f"Dependency, build or runner configuration cannot be overlaid: {path}.")
    suffix = name[name.rfind("."):] if "." in name else ""
    if suffix not in _ALLOWED_EXTENSIONS:
        raise SecurityError(f"Overlay file type is not permitted: {path}.")
    return path


def _header_hint(line: str) -> str:
    if line.startswith(("From ", "Subject:", "Date:", "---", "Signed-off-by:")):
        return "This looks like format-patch or commit text; supply only the unified diff (for example `git diff` output)."
    if not line.strip():
        return "Remove blank lines between file sections and before the first header."
    return "Each file section must start with `diff --git a/<path> b/<path>` (identical, unquoted paths)."


def _scan_body(lines: list[str], start: int) -> tuple[int, int, int]:
    """Count old/new/total body lines from `start` up to the next section, for exact count reporting."""
    old = new = 0
    i = start
    while i < len(lines) and not lines[i].startswith(_SECTION_START) and lines[i][:1] in (" ", "+", "-", "\\"):
        old += lines[i][:1] in " -"
        new += lines[i][:1] in " +"
        i += 1
    return old, new, i - start


def validate_overlay(patch: Any, test_dirs: tuple[str, ...], *, max_bytes: int) -> OverlaySummary:
    """Parse strictly: only regular-file additions and modifications, exact hunk counts, no metadata changes.

    Every rejection names its stage and failing line (see `_reject`). The parser accepts exactly what it
    always accepted; the only normalization is dropping blank lines after the final hunk.
    """
    if not isinstance(patch, str) or not patch.strip():
        raise ValidationError("test_overlay must be a non-empty unified diff.", {"stage": "input"})
    try:
        patch.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("Overlay must be valid UTF-8.", {"stage": "input"}) from exc
    if "\0" in patch or "\r" in patch:
        raise SecurityError("Overlay contains NUL or carriage-return characters (use LF line endings).",
                            {"stage": "input", "has_nul": "\0" in patch, "has_cr": "\r" in patch})
    lines = patch.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    canonical = "\n".join(lines) + "\n"
    encoded = canonical.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValidationError(f"Overlay exceeds the {max_bytes}-byte limit.", {"stage": "input", "bytes": len(encoded), "max_bytes": max_bytes})
    files: list[str] = []
    new_files: list[str] = []
    added = removed = 0
    i = 0
    while i < len(lines):
        match = _FILE_HEADER.fullmatch(lines[i])
        if not match:
            _reject(ValidationError, "file_header" if not files else "trailing_content",
                    _header_hint(lines[i]) if not files else
                    "Unexpected content after the previous file section ended. " + _header_hint(lines[i]) +
                    " If this follows a hunk, the hunk header's line counts may be too small.", lines, i)
        if match[1] != match[2]:
            _reject(ValidationError, "file_header", "Renames and differing paths are not permitted; a and b paths must be identical.", lines, i)
        try:
            path = validate_overlay_path(match[1], test_dirs)
        except SecurityError as exc:
            _reject(SecurityError, "path", exc.message, lines, i)
        if path in files:
            _reject(ValidationError, "file_header", f"Duplicate path in overlay: {path}.", lines, i)
        files.append(path)
        if len(files) > MAX_FILES:
            _reject(ValidationError, "limits", f"Overlay touches more than {MAX_FILES} files.", lines, i)
        i += 1
        is_new = False
        if i < len(lines) and lines[i].startswith("new file mode "):
            if lines[i] != "new file mode 100644":
                _reject(SecurityError, "file_mode", "New files must be regular and non-executable (mode 100644).", lines, i)
            is_new = True
            new_files.append(path)
            i += 1
        if i < len(lines) and lines[i].startswith("index "):
            if not re.fullmatch(r"index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}(?: 100644)?", lines[i]):
                _reject(SecurityError, "file_mode", "Unsupported index header (modes other than 100644 are rejected).", lines, i)
            i += 1
        if i < len(lines) and re.match(r"(deleted file|old mode|new mode|rename |copy |similarity |dissimilarity |Binary|GIT binary)", lines[i]):
            _reject(SecurityError, "file_mode", "Deletions, renames, copies, mode changes and binary changes are not permitted.", lines, i)
        expected = ["--- /dev/null" if is_new else f"--- a/{path}", f"+++ b/{path}"]
        if lines[i:i + 2] != expected:
            _reject(ValidationError, "file_markers", f"Expected the markers {expected[0]!r} then {expected[1]!r}.", lines, i)
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@"):
            header = _HUNK_HEADER_RE.match(lines[i])
            if not header:
                _reject(ValidationError, "hunk_header", "Malformed hunk header; expected `@@ -a,b +c,d @@`.", lines, i)
            if is_new and (hunks or header[1] != "0" or header[2] != "0" or header[3] != "1"):
                _reject(ValidationError, "hunk_header", "A new file must be a single hunk of the form @@ -0,0 +1,N @@.", lines, i)
            old_left, new_left = int(header[2] or 1), int(header[4] or 1)
            declared = (old_left, new_left)
            if old_left + new_left == 0:
                _reject(ValidationError, "hunk_header", "Empty hunk.", lines, i)
            hunk_line = i
            i += 1
            body_start = i
            while old_left or new_left:
                if i >= len(lines) or lines[i][:1] not in (" ", "+", "-") or lines[i].startswith(_SECTION_START):
                    found_old, found_new, _ = _scan_body(lines, body_start)
                    hint = ("An empty line inside a hunk must be written as a single space (context) or with its +/- marker."
                            if i < len(lines) and not lines[i] else
                            "The hunk header declares more lines than the body contains.")
                    _reject(ValidationError, "hunk_count", f"The hunk declares {declared[0]} old and {declared[1]} new lines "
                            f"but the body ends after {found_old} old and {found_new} new. {hint}", lines, i if i < len(lines) else len(lines) - 1,
                            hunk_line=hunk_line + 1, declared={"old": declared[0], "new": declared[1]},
                            found={"old": found_old, "new": found_new})
                kind = lines[i][0]
                old_left -= kind in " -"
                new_left -= kind in " +"
                added += kind == "+"
                removed += kind == "-"
                if min(old_left, new_left) < 0 or (is_new and kind != "+"):
                    found_old, found_new, _ = _scan_body(lines, body_start)
                    _reject(ValidationError, "hunk_count", f"The hunk declares {declared[0]} old and {declared[1]} new lines "
                            f"but the body has at least {found_old} old and {found_new} new.", lines, i,
                            hunk_line=hunk_line + 1, declared={"old": declared[0], "new": declared[1]},
                            found={"old": found_old, "new": found_new})
                i += 1
                if i < len(lines) and lines[i] == r"\ No newline at end of file":
                    i += 1
            if i < len(lines) and lines[i][:1] in (" ", "+", "-") and not lines[i].startswith(_SECTION_START):
                found_old, found_new, _ = _scan_body(lines, body_start)
                _reject(ValidationError, "hunk_count", f"The hunk declares {declared[0]} old and {declared[1]} new lines but its body "
                        f"continues with {found_old} old and {found_new} new. Correct header: "
                        f"@@ -{header[1]},{found_old} +{header[3]},{found_new} @@", lines, i,
                        hunk_line=hunk_line + 1, declared={"old": declared[0], "new": declared[1]},
                        found={"old": found_old, "new": found_new})
            hunks += 1
        if not hunks:
            _reject(ValidationError, "hunk_header", "Missing change hunk.", lines, min(i, len(lines) - 1))
    if not files or added + removed == 0:
        raise ValidationError("Overlay has no changes.", {"stage": "input"})
    if added + removed > MAX_CHANGED_LINES:
        raise ValidationError(f"Overlay changes more than {MAX_CHANGED_LINES} lines.", {"stage": "limits"})
    return OverlaySummary(tuple(files), tuple(new_files), added, removed, hashlib.sha256(encoded).hexdigest(), len(encoded), canonical)


def apply_overlay(work: GitWorkspace, tree: str, summary: OverlaySummary, patch: str) -> str:
    """Apply to the index only and return the effective tree. No checkout, commit, ref or push is made."""
    work.git("read-tree", tree)
    for path in summary.files:
        entry = work.git("ls-files", "--stage", "-z", "--", path).decode().split("\0")[:-1]
        if path in summary.new_files:
            if entry:
                raise ValidationError(f"Overlay creates {path}, which already exists in the tested tree.", {"stage": "apply", "path": path})
        elif len(entry) != 1 or not entry[0].startswith("100644 ") or not entry[0].endswith("\t" + path):
            raise SecurityError(f"Overlay may modify only existing regular non-executable files: {path}.", {"stage": "apply", "path": path})
    data = (summary.canonical or patch).encode("utf-8")
    try:
        work.git("apply", "--cached", "--check", "--whitespace=nowarn", "-", input=data)
        work.git("apply", "--cached", "--whitespace=nowarn", "-", input=data)
    except GitOperationError as exc:
        raise ValidationError(f"The overlay does not apply to the exact tested tree: {exc.message}",
                              {"stage": "apply", "git_message": safe_text(exc.message, 600),
                               "hint": "The context lines must match the tested tree exactly; read the files at that revision and regenerate the diff."}) from exc
    raw = work.git("diff", "--cached", "--raw", "-z", "--no-renames", tree).split(b"\0")
    changed: dict[str, str] = {}
    for meta, path in zip(raw[0::2], raw[1::2]):
        if not meta:
            break
        fields = meta.decode().lstrip(":").split()
        old_mode, new_mode, status = fields[0], fields[1], fields[4][0]
        if status not in "AM" or new_mode != "100644" or (status == "M" and old_mode != "100644"):
            raise SecurityError("Overlay produced a change other than regular-file addition or modification.")
        changed[path.decode("utf-8", errors="replace")] = status
    if set(changed) != set(summary.files) or {p for p, s in changed.items() if s == "A"} != set(summary.new_files):
        raise SecurityError("Applied paths do not match the validated overlay.")
    for path in summary.files:
        blob = work.git("show", ":" + path)
        if len(blob) > MAX_RESULT_FILE_BYTES or b"\0" in blob:
            raise ValidationError(f"Overlay result for {path} is binary or too large.")
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"Overlay result for {path} is not UTF-8 text.") from exc
    return work.git("write-tree").decode().strip()
