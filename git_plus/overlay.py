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

from git_plus.errors import GitOperationError, SecurityError, ValidationError
from git_plus.git_workspace import GitWorkspace
from git_plus.quick_fix import _HUNK_HEADER_RE

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

    @property
    def test_files(self) -> tuple[str, ...]:
        """Overlay files pytest would collect; the result must show these actually ran."""
        return tuple(p for p in self.files if _TEST_FILE.fullmatch(p.rsplit("/", 1)[-1]))


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


def validate_overlay(patch: Any, test_dirs: tuple[str, ...], *, max_bytes: int) -> OverlaySummary:
    """Parse strictly: only regular-file additions and modifications, exact hunk counts, no metadata changes."""
    if not isinstance(patch, str) or not patch.strip():
        raise ValidationError("test_overlay must be a non-empty unified diff.")
    try:
        encoded = patch.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("Overlay must be valid UTF-8.") from exc
    if len(encoded) > max_bytes:
        raise ValidationError(f"Overlay exceeds the {max_bytes}-byte limit.")
    if "\0" in patch or "\r" in patch:
        raise SecurityError("Overlay contains NUL or carriage-return characters.")
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    files: list[str] = []
    new_files: list[str] = []
    added = removed = 0
    i = 0
    while i < len(lines):
        match = re.fullmatch(r"diff --git a/([^\s\"\\]+) b/([^\s\"\\]+)", lines[i])
        if not match or match[1] != match[2]:
            raise ValidationError("Expected 'diff --git a/<path> b/<path>' with identical unquoted paths.")
        path = validate_overlay_path(match[1], test_dirs)
        if path in files:
            raise ValidationError("Duplicate path in overlay.")
        files.append(path)
        if len(files) > MAX_FILES:
            raise ValidationError(f"Overlay touches more than {MAX_FILES} files.")
        i += 1
        is_new = False
        if i < len(lines) and lines[i].startswith("new file mode "):
            if lines[i] != "new file mode 100644":
                raise SecurityError("New files must be regular and non-executable (mode 100644).")
            is_new = True
            new_files.append(path)
            i += 1
        if i < len(lines) and lines[i].startswith("index "):
            if not re.fullmatch(r"index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}(?: 100644)?", lines[i]):
                raise SecurityError("Unsupported index header (modes other than 100644 are rejected).")
            i += 1
        if i < len(lines) and re.match(r"(deleted file|old mode|new mode|rename |copy |similarity |dissimilarity |Binary|GIT binary)", lines[i]):
            raise SecurityError("Deletions, renames, copies, mode changes and binary changes are not permitted.")
        expected = ["--- /dev/null" if is_new else f"--- a/{path}", f"+++ b/{path}"]
        if lines[i:i + 2] != expected:
            raise ValidationError("Missing or mismatched file headers.")
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@"):
            header = _HUNK_HEADER_RE.match(lines[i])
            if not header:
                raise ValidationError("Malformed hunk header.")
            if is_new and (hunks or header[1] != "0" or header[2] != "0" or header[3] != "1"):
                raise ValidationError("A new file must be a single hunk of the form @@ -0,0 +1,N @@.")
            old_left, new_left = int(header[2] or 1), int(header[4] or 1)
            if old_left + new_left == 0:
                raise ValidationError("Empty hunk.")
            i += 1
            while old_left or new_left:
                if i >= len(lines) or not lines[i] or lines[i][0] not in " +-":
                    raise ValidationError("Malformed hunk body or line count.")
                kind = lines[i][0]
                old_left -= kind in " -"
                new_left -= kind in " +"
                added += kind == "+"
                removed += kind == "-"
                if min(old_left, new_left) < 0 or (is_new and kind != "+"):
                    raise ValidationError("Hunk line counts do not match.")
                i += 1
                if i < len(lines) and lines[i] == r"\ No newline at end of file":
                    i += 1
            hunks += 1
        if not hunks:
            raise ValidationError("Missing change hunk.")
    if not files or added + removed == 0:
        raise ValidationError("Overlay has no changes.")
    if added + removed > MAX_CHANGED_LINES:
        raise ValidationError(f"Overlay changes more than {MAX_CHANGED_LINES} lines.")
    return OverlaySummary(tuple(files), tuple(new_files), added, removed, hashlib.sha256(encoded).hexdigest(), len(encoded))


def apply_overlay(work: GitWorkspace, tree: str, summary: OverlaySummary, patch: str) -> str:
    """Apply to the index only and return the effective tree. No checkout, commit, ref or push is made."""
    work.git("read-tree", tree)
    for path in summary.files:
        entry = work.git("ls-files", "--stage", "-z", "--", path).decode().split("\0")[:-1]
        if path in summary.new_files:
            if entry:
                raise ValidationError(f"Overlay creates {path}, which already exists in the tested tree.")
        elif len(entry) != 1 or not entry[0].startswith("100644 ") or not entry[0].endswith("\t" + path):
            raise SecurityError(f"Overlay may modify only existing regular non-executable files: {path}.")
    data = patch.encode("utf-8")
    try:
        work.git("apply", "--cached", "--check", "--whitespace=nowarn", "-", input=data)
        work.git("apply", "--cached", "--whitespace=nowarn", "-", input=data)
    except GitOperationError as exc:
        raise ValidationError(f"The overlay does not apply to the exact tested tree: {exc.message}") from exc
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
