"""Strict bounded patches, applied atomically to the exact PR head."""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from odytor.errors import APIError, GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from odytor.github import GitHubClient, validate_repo, validate_sha
from odytor.git_workspace import GitWorkspace, pr_context, verify_context

# ---- Size limits ----
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_CHANGED_LINES = 1000
DEFAULT_MAX_BYTES = 262144
# Hard ceilings for operator configuration (see Settings.load).
MAX_FILES_CEILING = 50
MAX_CHANGED_LINES_CEILING = 5000
MAX_BYTES_CEILING = 1048576

# ---- Path security patterns ----
_PROHIBITED_DIRS = frozenset({
    ".git", ".github",
    "deploy", "deployment", "deployments",
    "k8s", "kubernetes", "terraform", "ansible", "helm", "helm-charts",
    "bin", "scripts", "script",
})

_SENSITIVE_KEYWORDS = (
    "auth", "oauth", "secret", "credential", "password",
    "token", "jwt", "login", "session", "cookie",
    "permission", "privilege", "crypt", "ssrf",
)

# Exact, repository-scoped path exceptions for false positives on ordinary source files.
# Bypasses the sensitive keyword pattern check only; traversal, extension, directory,
# and protected-file restrictions remain strictly enforced.
_REPO_PATH_KEYWORD_EXEMPTIONS: dict[str, frozenset[str]] = {
    "odysseus-dev/odysseus": frozenset({
        "frontend/src/features/notes/application/noteSession.js",
        "static/js/features/notes/application/noteSession.js",
    }),
}


def _component_words(component: str) -> set[str]:
    """Extract individual lowercase word tokens from a path component.

    Splits on non-alphanumeric characters and camelCase boundaries so that
    words like 'author' or 'tokenizer' do not trigger false positives on
    'auth' or 'token', while 'auth_middleware' or 'authMiddleware' are caught.
    """
    stem, _ = os.path.splitext(component)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem)
    tokens = re.split(r"[^a-zA-Z0-9]+", s)
    return {t.lower() for t in tokens if t}

_KEY_AND_CERT_EXTENSIONS = frozenset({
    ".key", ".pem", ".crt", ".cer", ".p12", ".pfx",
    ".keystore", ".jks", ".pub", ".asc", ".sig",
})

_SCRIPT_AND_BINARY_EXTENSIONS = frozenset({
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".psm1",
    ".exe", ".bin", ".elf", ".so", ".dll", ".dylib", ".o", ".a",
    ".pyc", ".pyd", ".class", ".jar",
})

_DEPENDENCY_LOCKFILES = frozenset({
    "requirements.txt", "pipfile", "pipfile.lock", "pyproject.toml",
    "setup.py", "setup.cfg", "poetry.lock", "uv.lock",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum", "go.work",
    "pom.xml", "gemfile", "gemfile.lock", "composer.json", "composer.lock",
})

_SENSITIVE_EXACT_FILES = frozenset({
    "htpasswd", "netrc", "ssh_config", "known_hosts", "sudoers",
})

_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)

@dataclass(frozen=True)
class PatchSummary:
    files: tuple[str, ...]
    added_lines: int
    removed_lines: int
    sha256: str
    patch_bytes: int


def _validate_path(path_str: str, repo: str | None = None, *, path_exemptions: tuple[str, ...] | None = None) -> str:
    """Enforce security policies on a file path in a patch."""
    if not path_str or not path_str.strip():
        raise ValidationError("File path cannot be empty.")
    if "\0" in path_str or "\\" in path_str:
        raise SecurityError(f"Path '{path_str}' contains forbidden characters.")

    for ch in path_str:
        code = ord(ch)
        if code < 32 or (127 <= code < 160):
            raise SecurityError(f"Path '{path_str}' contains control characters.")

    if path_str.startswith("/") or path_str.startswith("~"):
        raise SecurityError(f"Absolute or home-relative paths are forbidden: '{path_str}'.")
    if re.match(r"^[a-zA-Z]:", path_str):
        raise SecurityError(f"Windows drive paths are forbidden: '{path_str}'.")

    parts = path_str.split("/")
    if any(p == "" for p in parts):
        raise ValidationError(f"Path '{path_str}' has empty components.")
    if any(p in (".", "..") for p in parts):
        raise SecurityError(f"Directory traversal is forbidden: '{path_str}'.")
    for p in parts:
        if p.split(".")[0].upper() in _WINDOWS_DEVICE_NAMES:
            raise SecurityError(f"Windows reserved device name in path: '{path_str}'.")
        if p.lower() == ".github":
            raise SecurityError(f"github config paths are forbidden: '{path_str}'.")
        if p.lower().startswith(".git"):
            raise SecurityError(f"git metadata paths are forbidden: '{path_str}'.")
        if p == ".github":
            raise SecurityError(f"GitHub config paths are forbidden: '{path_str}'.")
        if p.lower() in _PROHIBITED_DIRS:
            raise SecurityError(f"Restricted directory '{p}' in path: '{path_str}'.")

    filename = parts[-1].lower()
    _, ext = os.path.splitext(filename)

    if filename == "dockerfile" or filename.startswith("dockerfile.") or filename.endswith(".dockerfile"):
        raise SecurityError(f"Container definitions are not permitted: '{path_str}'.")
    if re.match(r"^(docker-)?compose.*\.ya?ml$", filename):
        raise SecurityError(f"Docker Compose definitions are not permitted: '{path_str}'.")

    repo_exemptions = path_exemptions if path_exemptions is not None else _REPO_PATH_KEYWORD_EXEMPTIONS.get((repo or "").casefold(), ())
    if path_str not in repo_exemptions:
        for p in parts:
            words = _component_words(p)
            if any(kw in words for kw in _SENSITIVE_KEYWORDS):
                raise SecurityError(f"Path contains sensitive keyword pattern: '{path_str}'.")

    if ext in _KEY_AND_CERT_EXTENSIONS:
        raise SecurityError(f"Cryptographic key/cert files are not permitted: '{path_str}'.")
    if ext in _SCRIPT_AND_BINARY_EXTENSIONS:
        raise SecurityError(f"Executable script/binary files are not permitted: '{path_str}'.")
    if any(filename.startswith(prefix) for prefix in ("id_rsa", "id_ecdsa", "id_ed25519", "id_dsa", ".env")):
        raise SecurityError(f"Sensitive identity/environment files are not permitted: '{path_str}'.")
    if filename in _SENSITIVE_EXACT_FILES:
        raise SecurityError(f"Sensitive configuration file is not permitted: '{path_str}'.")
    if (
        filename in _DEPENDENCY_LOCKFILES
        or filename.startswith("requirements")
        or filename.startswith("build.gradle")
        or filename.startswith("settings.gradle")
    ):
        raise SecurityError(f"Dependency manifests and lockfiles are not permitted: '{path_str}'.")

    return path_str


def validate_patch(
    patch: str, *, repo: str | None = None, max_files: int = DEFAULT_MAX_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES, max_bytes: int = DEFAULT_MAX_BYTES,
    path_exemptions: tuple[str, ...] | None = None,
) -> PatchSummary:
    """Accept matching unquoted Git paths and count every hunk line exactly."""
    if any(type(v) is not int or v <= 0 for v in (max_files, max_changed_lines, max_bytes)):
        raise ValidationError("Patch limits must be positive integers.")
    if not isinstance(patch, str):
        raise ValidationError("Patch must be a string.")
    try:
        encoded = patch.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("Patch must be valid UTF-8.") from exc
    if len(encoded) > max_bytes:
        raise ValidationError("Patch size exceeds limit.")
    if not patch.strip():
        raise ValidationError("Patch is empty.")
    if "\0" in patch:
        raise SecurityError("Patch contains NUL characters.")
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    files = []
    added = removed = 0
    i = 0
    while i < len(lines):
        match = re.fullmatch(r"diff --git a/([^\s\"]+) b/([^\s\"]+)", lines[i])
        if not match or match[1] != match[2]:
            raise ValidationError("Expected matching unquoted diff --git paths.")
        path = _validate_path(match[1], repo=repo, path_exemptions=path_exemptions)
        if path in files:
            raise ValidationError("Duplicate diff path.")
        files.append(path)
        if len(files) > max_files:
            raise ValidationError("File count exceeding limit.")
        i += 1
        if i < len(lines) and lines[i].startswith("index "):
            if not re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?", lines[i]):
                raise ValidationError("Invalid or unsafe index header.")
            i += 1
        if i < len(lines) and lines[i].startswith("Binary"):
            raise ValidationError("Binary diffs are not supported.")
        if i < len(lines) and lines[i].startswith(("new file", "deleted file")):
            raise ValidationError("File creation and deletion are not supported.")
        if lines[i:i+2] != [f"--- a/{path}", f"+++ b/{path}"]:
            raise ValidationError("Missing or mismatched file headers; metadata changes are forbidden.")
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@"):
            h = _HUNK_HEADER_RE.match(lines[i])
            if not h:
                raise ValidationError("Malformed hunk header.")
            old_left = int(h[2] or 1)
            new_left = int(h[4] or 1)
            if old_left + new_left == 0:
                raise ValidationError("Empty hunk.")
            i += 1
            while old_left or new_left:
                if i >= len(lines) or not lines[i] or lines[i][0] not in " +-":
                    raise ValidationError("Malformed hunk body or line count.")
                kind = lines[i][0]
                if kind in " -":
                    old_left -= 1
                if kind in " +":
                    new_left -= 1
                added += kind == "+"
                removed += kind == "-"
                if min(old_left, new_left) < 0:
                    raise ValidationError("Hunk line counts do not match.")
                i += 1
                if i < len(lines) and lines[i] == r"\ No newline at end of file":
                    i += 1
            hunks += 1
        if not hunks:
            raise ValidationError("Missing change hunk.")
    if not files or not added + removed:
        raise ValidationError("Patch has no changes.")
    if added + removed > max_changed_lines:
        raise ValidationError("Changed line count exceeding limit.")
    return PatchSummary(tuple(files), added, removed, hashlib.sha256(encoded).hexdigest(), len(encoded))


_CREATE_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url tree { oid } parents(first: 2) { totalCount nodes { oid } } }
  }
}
"""


def _commit_identity(client: GitHubClient) -> tuple[str, str]:
    """Attribute a Git-pushed commit to the authenticated GitHub login."""
    login = getattr(client, 'principal', None)
    if not login:
        login = client.request('GET', 'user').json().get('login')
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
        raise SecurityError("Authenticated GitHub login is unavailable for commit attribution.")
    return login, f"{login}@users.noreply.github.com"


def _push_fork_commit(work: GitWorkspace, repo: str, number: int, context: dict[str, Any],
                      summary: PatchSummary, tree: str, commit_message: str) -> dict[str, Any]:
    """Fast-forward a verified contributor branch with one commit, then verify the remote."""
    head_repo, head_ref, head_sha = context["head_repo"], context["head_ref"], context["head_sha"]
    new_sha = validate_sha(work.git("commit-tree", tree, "-p", head_sha, "-m", commit_message.strip()).decode().strip())
    work.push_fast_forward(head_repo, head_ref, head_sha, new_sha, "quick_fix")
    remote_ref = f"refs/heads/{head_ref}"
    try:
        remote = work.git("ls-remote", "--refs", "--", work.url(head_repo), remote_ref,
                          network=True, credential_repo=head_repo).decode().split()
    except GitOperationError as exc:
        raise GitOperationError("Push accepted, remote verification unavailable. Reconcile before retrying.", {
            "outcome": "unknown", "new_sha": new_sha, "head_repo": head_repo,
        }) from exc
    if not remote or remote[0] != new_sha:
        raise GitOperationError("Push returned success but remote revision differs; reconcile before retrying.", {
            "outcome": "unknown", "new_sha": new_sha, "observed_sha": remote[0] if remote else None,
        })
    return {
        "applied": True, "repo": repo, "number": number, "head_repo": head_repo, "head_ref": head_ref,
        "expected_head": head_sha, "new_sha": new_sha,
        "commit_url": f"{work.origin()}/{head_repo}/commit/{new_sha}", "tree_sha": tree,
        "changed_files": list(summary.files), "added_lines": summary.added_lines,
        "removed_lines": summary.removed_lines, "patch_sha256": summary.sha256, "is_fork": True,
        "mechanism": "git_push_fast_forward", "remote_verified": True, "signed": False,
        "validation": {"patch_applies": True, "paths_and_modes": True, "tests_run": False},
    }


def apply_quick_fix(
    client: GitHubClient, repo: str, number: int, expected_head: str,
    patch: str, commit_message: str, *, max_files: int = DEFAULT_MAX_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES, max_bytes: int = DEFAULT_MAX_BYTES,
    path_exemptions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate locally, then append one commit with GitHub's atomic head check.

    GitHub attributes the commit to the authenticated identity. No repository
    code or tests run. A lost mutation response requires reconciliation.
    """
    summary = validate_patch(patch, repo=repo, max_files=max_files, max_changed_lines=max_changed_lines, max_bytes=max_bytes,
                             path_exemptions=path_exemptions)
    if not commit_message.strip() or len(commit_message.encode()) > 1440 or "\0" in commit_message:
        raise ValidationError("Commit message must contain 1-1440 bytes without NUL.")
    context = pr_context(client, repo, number, expected_head, write=True, operation="quick_fix")
    identity = _commit_identity(client) if context["is_fork"] else None
    with GitWorkspace(client, identity=identity) as work:
        work.fetch(context["head_repo"], context["head_ref"], context["head_sha"], "head", depth=1)
        work.git("read-tree", context["head_sha"])
        for path in summary.files:
            entry = work.git("ls-files", "--stage", "-z", "--", path).decode().split("\0")
            if len(entry) != 2 or not entry[0].startswith("100644 ") or not entry[0].endswith("\t" + path):
                raise SecurityError("Patch targets must be existing regular non-executable files.")
        work.git("apply", "--cached", "--check", "--whitespace=nowarn", "-", input=patch.encode())
        work.git("apply", "--cached", "--whitespace=nowarn", "-", input=patch.encode())
        changed = work.git("diff", "--cached", "--name-only", "-z", context["head_sha"]).decode().strip("\0").split("\0")
        if set(changed) != set(summary.files):
            raise SecurityError("Applied paths do not match validated patch.")
        additions = []
        total = 0
        for path in summary.files:
            blob = work.git("show", ":" + path)
            total += len(blob)
            if total > 1048576 or b"\0" in blob:
                raise ValidationError("Patched content is binary or exceeds 1 MiB.")
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("Patched files must contain UTF-8 text.") from exc
            additions.append({"path": path, "contents": base64.b64encode(blob).decode()})
        tree = work.git("write-tree").decode().strip()
        verify_context(client, repo, number, context, write=True, operation="quick_fix")
        if context["is_fork"]:
            return _push_fork_commit(work, repo, number, context, summary, tree, commit_message)
        headline, _, body = commit_message.strip().partition("\n")
        try:
            data = client.graphql(_CREATE_COMMIT, {"input": {
                "branch": {"repositoryNameWithOwner": context["head_repo"], "branchName": context["head_ref"]},
                "expectedHeadOid": context["head_sha"],
                "message": {"headline": headline, "body": body},
                "fileChanges": {"additions": additions},
            }})
        except APIError as exc:
            raise APIError("Commit request failed; reconcile the PR head before retrying.", diagnostic={
                "outcome": "unknown", "expected_head": context["head_sha"], "head_repo": context["head_repo"],
            }) from exc
        commit = (data.get("createCommitOnBranch") or {}).get("commit") or {}
        parents = commit.get("parents") or {}
        if (commit.get("tree", {}).get("oid") != tree or parents.get("totalCount") != 1
                or parents.get("nodes") != [{"oid": context["head_sha"]}]):
            raise GitOperationError("Commit response did not verify; reconcile before retrying.", {
                "outcome": "unknown", "commit": commit.get("oid"),
            })
        new_sha = validate_sha(commit.get("oid", ""))
        return {
            "applied": True, "repo": repo, "number": number, "head_repo": context["head_repo"],
            "head_ref": context["head_ref"], "expected_head": context["head_sha"], "new_sha": new_sha,
            "commit_url": commit.get("url"), "tree_sha": tree, "changed_files": list(summary.files),
            "added_lines": summary.added_lines, "removed_lines": summary.removed_lines,
            "patch_sha256": summary.sha256, "is_fork": context["is_fork"],
            "validation": {"patch_applies": True, "paths_and_modes": True, "tests_run": False},
        }
