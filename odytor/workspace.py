"""Disposable engineering workspaces: exact-revision source an agent can read, search, edit, diff and test.

A workspace is a small private Git object store plus a JSON state record. Its working state is a *tree
object* and a monotonically increasing version, never a checked-out directory: paths exist only as tree
entries, so there is no filesystem to escape from and no symlink to follow. Reads (`ls-tree`, `cat-file`,
`grep <tree>`) and writes (`update-index --cacheinfo`, `write-tree`) go through hardened Git
(`GitWorkspace`: no inherited config, hooks, filters or credentials) and never execute repository code.

Concurrency: every mutation holds an exclusive per-workspace file lock and must name the version it was
based on (`expected_version`); a stale caller gets a VersionConflictError carrying the current version and
tree and changes nothing. Reads see an atomically replaced state record and immutable objects.

Nothing here talks to GitHub except creation (fetch) and publication (a separate module).
"""
from __future__ import annotations

import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from odytor.config import WorkspaceSettings
from odytor.diagnostics import safe_text
from odytor.errors import GitOperationError, GitPlusError, SecurityError, ValidationError
from odytor.git_workspace import GitWorkspace
from odytor.github import validate_sha
from odytor.overlay import MAX_CHANGED_LINES, parse_patch
from odytor.procs import run_bounded

SCHEMA = 1
POLICY_VERSION = 1
WS_ID = re.compile(r"ws-[0-9a-f]{32}")
CHECKPOINT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")
ALL_OPERATIONS = ("read", "search", "edit", "diff", "execute", "artifacts", "manage", "publish")
# "publish" is the only operation that can lead to a GitHub write, so a workspace never has it unless the creator asks for it by name.
DEFAULT_OPERATIONS = tuple(op for op in ALL_OPERATIONS if op != "publish")
MAX_HISTORY = 50
MAX_PATH_BYTES = 400
READ_MAX_LINES = 400
READ_MAX_BYTES = 65536
SEARCH_MAX_RESULTS = 100
TREE_MAX_ENTRIES = 500
SYMBOL_MAX_BYTES = 256 * 1024
_FORBIDDEN_WRITE_COMPONENTS = frozenset({".git", ".gitmodules"})
_REGULAR_MODES = frozenset({"100644", "100755"})


class VersionConflictError(GitPlusError):
    """The caller edited from a stale workspace version; nothing was changed."""

    def __init__(self, expected: int, current: int, tree: str) -> None:
        super().__init__(f"Workspace version conflict: expected {expected}, current is {current}. "
                         "Re-read the workspace state and reapply the change; nothing was modified.",
                         {"expected_version": expected, "current_version": current, "current_tree": tree})


def new_workspace_id() -> str:
    return "ws-" + os.urandom(16).hex()


def validate_path(path: Any, *, write: bool = False) -> str:
    """A plain repository-relative path. Nothing else ever reaches a Git argument or a tar header."""
    if not isinstance(path, str) or not path:
        raise ValidationError("path must be a non-empty repository-relative string.", {"stage": "path"})
    if len(path.encode("utf-8", errors="replace")) > MAX_PATH_BYTES:
        raise ValidationError("path is too long.", {"stage": "path"})
    if path.startswith("/") or path.endswith("/") or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise SecurityError("path must be relative, use '/' separators and contain no control characters.", {"stage": "path"})
    parts = path.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise SecurityError("path must not contain empty, '.' or '..' components.", {"stage": "path"})
    if any(p.casefold() == ".git" for p in parts):
        raise SecurityError("paths inside .git are not addressable.", {"stage": "path"})
    if write and any(p.casefold() in _FORBIDDEN_WRITE_COMPONENTS for p in parts):
        raise SecurityError(f"{parts[-1]} cannot be modified in a workspace.", {"stage": "path"})
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("path must be valid UTF-8.", {"stage": "path"}) from exc
    return path


def _write_path(path: str) -> str:
    return validate_path(path, write=True)


class WorkspaceRepo(GitWorkspace):
    """The hardened GitWorkspace, pointed at a durable directory that is neither created nor removed per call."""

    def __init__(self, directory: Path, client: Any = None) -> None:
        super().__init__(client)
        self.root = directory
        self.path = directory / "repo"

    def __enter__(self) -> WorkspaceRepo:
        if not (self.path / ".git").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self.git("init", "--quiet", "--template=")
        return self

    def __exit__(self, *exc: Any) -> None:  # durable: the service decides when a workspace is removed
        return None

    def stream(self, *args: str, timeout: float = 20.0, max_bytes: int = 1 << 20) -> tuple[bytes, bool, int | None]:
        """Run a read-only git command with a hard output and time bound; returns (stdout, overflowed, returncode)."""
        result = run_bounded(self.command("-C", str(self.path), *args), env=self.environment(), timeout=timeout, max_stdout=max_bytes)
        if result.timed_out:
            raise GitOperationError("The search timed out; narrow the pattern or the path.", {"stage": "timeout"})
        return result.stdout, result.overflowed, result.returncode


def _iso(epoch: float | None) -> str | None:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds") if epoch else None


def _numbered(text: str, start: int) -> list[dict[str, Any]]:
    return [{"n": start + i, "text": line} for i, line in enumerate(text.split("\n"))]


class WorkspaceService:
    """Durable, bounded workspaces owned by one authenticated caller each."""

    def __init__(self, cfg: WorkspaceSettings, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self.root = Path(cfg.data_dir)
        self._last_purge = 0.0
        self._guard = threading.Lock()  # fcntl locks are per process; this serializes threads inside it
        self._locks: dict[str, threading.Lock] = {}
        self._started = False

    # --- storage ---------------------------------------------------------------------------
    def start(self) -> None:
        if not self._started:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            self._started = True

    def _dir(self, ws_id: str) -> Path:
        if not isinstance(ws_id, str) or not WS_ID.fullmatch(ws_id):
            raise ValidationError("Unknown workspace_id.")
        return self.root / ws_id

    def _load(self, ws_id: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._dir(ws_id) / "state.json").read_text())
        except (OSError, ValueError, ValidationError):
            return None

    def _save(self, record: dict[str, Any]) -> None:
        target = self._dir(record["workspace_id"]) / "state.json"
        tmp = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, separators=(",", ":"))
        os.replace(tmp, target)

    def records(self) -> list[dict[str, Any]]:
        found = []
        for state in self.root.glob("ws-*/state.json"):
            try:
                found.append(json.loads(state.read_text()))
            except (OSError, ValueError):
                continue
        return found

    def _thread_lock(self, ws_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(ws_id, threading.Lock())

    @contextlib.contextmanager
    def _exclusive(self, ws_id: str) -> Iterator[None]:
        """Serialize mutations across threads and across processes that share the data directory."""
        directory = self._dir(ws_id)
        with self._thread_lock(ws_id):
            fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def get(self, ws_id: str, owner: str, *, operation: str) -> dict[str, Any]:
        """The caller's live workspace, or one indistinguishable error for unknown, foreign and expired ids."""
        self.start()
        self.purge()
        record = self._load(ws_id)
        if record is None or record["owner"] != owner or record["expires_at"] <= self.clock():
            raise ValidationError("Unknown workspace_id.")
        if operation not in record["scope"]["operations"]:
            raise SecurityError(f"This workspace's authorization scope does not include '{operation}'.",
                                {"allowed_operations": record["scope"]["operations"], "policy_version": POLICY_VERSION})
        return record

    # --- lifecycle -------------------------------------------------------------------------
    def create(self, *, owner: str, repo: str, source: dict[str, Any], populate: Callable[[WorkspaceRepo], str],
               pr: int | None, operations: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Reserve space, let `populate` fetch the objects, and record the workspace atomically (state.json last)."""
        self.start()
        self.purge(force=True)
        live = self.records()
        mine = [r for r in live if r["owner"] == owner and r["expires_at"] > self.clock()]
        if len(mine) >= self.cfg.max_per_owner:
            raise ValidationError("Workspace limit reached; close one with workspace_manage(action='close') first.",
                                  {"max_per_owner": self.cfg.max_per_owner, "open": [r["workspace_id"] for r in mine]})
        if sum(r.get("bytes", 0) for r in live) + self.cfg.max_workspace_bytes > self.cfg.max_total_bytes:
            raise ValidationError("The workspace store is full; retry after workspaces expire or are closed.")
        if shutil.disk_usage(self.root).free < self.cfg.min_free_bytes + self.cfg.max_workspace_bytes:
            raise ValidationError("Insufficient disk space to create a workspace.")
        scope = tuple(dict.fromkeys(operations or DEFAULT_OPERATIONS))
        if not set(scope) <= set(ALL_OPERATIONS):
            raise ValidationError(f"operations must be a subset of: {', '.join(ALL_OPERATIONS)}.")
        ws_id = new_workspace_id()
        directory = self._dir(ws_id)
        directory.mkdir(mode=0o700)
        try:
            with WorkspaceRepo(directory) as repository:
                tree = populate(repository)
                size = self._repo_bytes(repository)
                if size > self.cfg.max_workspace_bytes:
                    raise ValidationError("The repository exceeds the workspace size limit.", {"max_bytes": self.cfg.max_workspace_bytes})
                repository.git("update-ref", "refs/odytor/current", tree)
                repository.git("update-ref", "refs/odytor/source", tree)
                files = self._count_files(repository, tree)
            now = self.clock()
            record = {"schema": SCHEMA, "workspace_id": ws_id, "owner": owner, "repo": repo, "pr": pr, "source": source,
                      "source_tree": tree, "tree": tree, "version": 0, "checkpoints": {}, "history": [],
                      "created_at": now, "updated_at": now, "expires_at": now + self.cfg.ttl_hours * 3600,
                      "bytes": size, "files": files, "runs": [], "artifacts": [],
                      "limits": self.limits(), "scope": {"operations": list(scope), "policy_version": POLICY_VERSION}}
            self._save(record)
            return record
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def limits(self) -> dict[str, Any]:
        c = self.cfg
        return {"ttl_hours": c.ttl_hours, "max_ttl_hours": c.max_ttl_hours, "max_workspace_mib": c.max_workspace_bytes // (1 << 20),
                "max_file_bytes": c.max_file_bytes, "max_changed_files": c.max_changed_files, "max_checkpoints": c.max_checkpoints,
                "read_max_lines": READ_MAX_LINES, "read_max_bytes": READ_MAX_BYTES, "search_max_results": SEARCH_MAX_RESULTS}

    def close(self, ws_id: str, owner: str) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="manage")
        with self._exclusive(ws_id):
            shutil.rmtree(self._dir(ws_id), ignore_errors=True)
        return {"workspace_id": ws_id, "closed": True, "repo": record["repo"], "final_version": record["version"],
                "final_tree": record["tree"], "unpublished_changes": record["tree"] != record["source_tree"]}

    def extend(self, ws_id: str, owner: str, hours: float) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="manage")
        if not 0 < hours <= self.cfg.max_ttl_hours:
            raise ValidationError(f"hours must be between 0 and {self.cfg.max_ttl_hours}.")
        with self._exclusive(ws_id):
            record = self._load(ws_id) or record
            ceiling = record["created_at"] + self.cfg.max_ttl_hours * 3600
            record["expires_at"] = min(max(record["expires_at"], self.clock() + hours * 3600), ceiling)
            self._save(record)
        return record

    def peek(self, ws_id: str, owner: str) -> dict[str, Any] | None:
        """The caller's live workspace record, or None; unlike get() it is scope-free and never raises."""
        record = self._load(ws_id) if isinstance(ws_id, str) and WS_ID.fullmatch(ws_id) else None
        return record if record and record["owner"] == owner and record["expires_at"] > self.clock() else None

    def list_for(self, owner: str) -> list[dict[str, Any]]:
        self.start()
        self.purge()
        return [r for r in self.records() if r["owner"] == owner and r["expires_at"] > self.clock()]

    def purge(self, *, force: bool = False) -> int:
        """Remove expired workspaces and half-created debris; cheap enough to run lazily every minute."""
        now = self.clock()
        if not force and now - self._last_purge < 60:
            return 0
        self._last_purge = now
        removed = 0
        if not self.root.exists():
            return 0
        for directory in self.root.glob("ws-*"):
            state = directory / "state.json"
            try:
                expired = json.loads(state.read_text())["expires_at"] <= now
            except (OSError, ValueError, KeyError):
                expired = now - directory.stat().st_mtime > 3600  # a create that never finished
            if expired:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    # --- helpers ---------------------------------------------------------------------------
    def _repo(self, ws_id: str) -> WorkspaceRepo:
        return WorkspaceRepo(self._dir(ws_id)).__enter__()

    @staticmethod
    def _repo_bytes(repository: WorkspaceRepo) -> int:
        out = repository.git("count-objects", "-v").decode()
        values = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        return (int(values.get("size", 0)) + int(values.get("size-pack", 0)) + int(values.get("size-garbage", 0))) * 1024

    @staticmethod
    def _count_files(repository: WorkspaceRepo, tree: str) -> int:
        return repository.git("ls-tree", "-r", "-z", "--name-only", tree).count(b"\0")

    def _entry(self, repository: WorkspaceRepo, tree: str, path: str) -> tuple[str, str, str, int | None] | None:
        """(mode, type, oid, size) of one exact path in a tree, or None."""
        out = repository.git("ls-tree", "-z", "-l", tree, "--", path)
        for raw in filter(None, out.split(b"\0")):
            meta, _, name = raw.partition(b"\t")
            if name.decode("utf-8", errors="replace") == path:
                mode, kind, oid, size = meta.decode().split()
                return mode, kind, oid, None if size == "-" else int(size)
        return None

    def _require_parents_are_directories(self, repository: WorkspaceRepo, tree: str, path: str) -> None:
        parts = path.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if not prefixes:
            return
        out = repository.git("ls-tree", "-z", tree, "--", *prefixes)
        for raw in filter(None, out.split(b"\0")):
            meta, _, name = raw.partition(b"\t")
            if meta.split()[1] != b"tree":
                raise ValidationError(f"{name.decode('utf-8', errors='replace')} is a file or link, so nothing can be created beneath it.",
                                      {"stage": "path", "path": path})

    # --- reads -----------------------------------------------------------------------------
    def summary(self, record: dict[str, Any]) -> dict[str, Any]:
        return {"workspace_id": record["workspace_id"], "repo": record["repo"], "pr": record["pr"], "source": record["source"],
                "version": record["version"], "tree": record["tree"], "source_tree": record["source_tree"],
                "modified": record["tree"] != record["source_tree"], "checkpoints": sorted(record["checkpoints"]),
                "created_at": _iso(record["created_at"]), "updated_at": _iso(record["updated_at"]),
                "expires_at": _iso(record["expires_at"]), "files": record.get("files"),
                "storage_bytes": record.get("bytes"), "limits": record["limits"], "scope": record["scope"]}

    def tree(self, ws_id: str, owner: str, *, path: str = "", recursive: bool = False,
             max_entries: int = TREE_MAX_ENTRIES) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="read")
        if path:
            validate_path(path)
        limit = min(max(int(max_entries), 1), TREE_MAX_ENTRIES)
        repository = self._repo(ws_id)
        args = ["ls-tree", "-z", "-l", *(["-r"] if recursive else []), record["tree"], *(["--", path + "/"] if path else [])]
        listing = repository.git(*args)
        entries, total = [], 0
        for raw in filter(None, listing.split(b"\0")):
            total += 1
            if len(entries) < limit:
                meta, _, name = raw.partition(b"\t")
                mode, kind, oid, size = meta.decode().split()
                entries.append({"path": name.decode("utf-8", errors="replace"), "type": {"blob": "file", "tree": "dir", "commit": "submodule"}[kind],
                                "mode": mode, "size": None if size == "-" else int(size), **({"symlink": True} if mode == "120000" else {})})
        return {**self._ident(record), "path": path or ".", "entries": entries, "entries_total": total,
                "complete": total <= limit}

    def _ident(self, record: dict[str, Any]) -> dict[str, Any]:
        return {"workspace_id": record["workspace_id"], "repo": record["repo"], "version": record["version"], "tree": record["tree"]}

    def read(self, ws_id: str, owner: str, *, path: str, start_line: int = 1, end_line: int | None = None,
             max_bytes: int = READ_MAX_BYTES) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="read")
        validate_path(path)
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValidationError("start_line must be >= 1 and end_line >= start_line.")
        repository = self._repo(ws_id)
        entry = self._entry(repository, record["tree"], path)
        if entry is None:
            raise ValidationError(f"No such file in the workspace: {path}", {"stage": "path"})
        mode, kind, oid, size = entry
        if kind != "blob":
            raise ValidationError(f"{path} is a {'directory' if kind == 'tree' else 'submodule'}; use workspace_read tree.", {"stage": "path"})
        if mode == "120000":
            target = repository.git("cat-file", "blob", oid)[:512].decode("utf-8", errors="replace")
            return {**self._ident(record), "path": path, "symlink_target": target, "type": "symlink"}
        cap = min(max(int(max_bytes), 256), READ_MAX_BYTES)
        data = repository.git("cat-file", "blob", oid)
        base = {**self._ident(record), "path": path, "mode": mode, "blob_sha": oid, "size_bytes": len(data)}
        if b"\0" in data[:8192]:
            return {**base, "binary": True, "lines": []}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {**base, "binary": True, "lines": [], "note": "Not valid UTF-8."}
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        stop = min(end_line or start_line + READ_MAX_LINES - 1, start_line + READ_MAX_LINES - 1, total)
        out, used = [], 0
        for number in range(start_line, stop + 1):
            line = lines[number - 1]
            used += len(line.encode("utf-8")) + 1
            if used > cap and out:
                stop = number - 1
                break
            out.append({"n": number, "text": line})
        return {**base, "binary": False, "total_lines": total, "start_line": start_line, "end_line": stop if out else start_line - 1,
                "lines": out, "complete": stop >= total and start_line == 1,
                "next_start_line": stop + 1 if stop < total else None, "ends_with_newline": text.endswith("\n")}

    def search(self, ws_id: str, owner: str, *, pattern: str, path: str = "", fixed: bool = True, ignore_case: bool = False,
               max_results: int = SEARCH_MAX_RESULTS) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="search")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200 or "\0" in pattern or "\n" in pattern:
            raise ValidationError("pattern must be a single-line string of 1-200 characters.")
        if path:
            validate_path(path)
        limit = min(max(int(max_results), 1), SEARCH_MAX_RESULTS)
        repository = self._repo(ws_id)
        args = ["grep", "-n", "-I", "-z", "--no-color", "-F" if fixed else "-E", *(["-i"] if ignore_case else []), "-e", pattern, record["tree"], "--", *([path] if path else [])]
        out, overflowed, code = repository.stream(*args, max_bytes=2 << 20)
        if code not in (0, 1):
            raise ValidationError("The search pattern was rejected by git grep." if not fixed else "The search failed.", {"stage": "search"})
        matches: list[dict[str, Any]] = []
        total = 0
        prefix = record["tree"] + ":"
        # git grep -z -n: "<tree>:<path>\0<line>\0<text>\n"
        for chunk in out.decode("utf-8", errors="replace").split("\n"):
            fields = chunk.split("\0", 2)
            if len(fields) != 3 or not fields[0].startswith(prefix) or not fields[1].isdigit():
                continue
            total += 1
            if len(matches) < limit:
                matches.append({"path": fields[0][len(prefix):], "line": int(fields[1]), "text": safe_text(fields[2][:240], 260)})
        return {**self._ident(record), "pattern": pattern, "fixed_string": fixed, "path": path or ".", "matches": matches,
                "matches_counted": total, "complete": total <= limit and not overflowed, "output_truncated": overflowed}

    def symbols(self, ws_id: str, owner: str, *, path: str) -> dict[str, Any]:
        """Definitions in one file: exact for Python (parsed, never executed), best-effort for JS/TS (pattern based)."""
        record = self.get(ws_id, owner, operation="read")
        validate_path(path)
        repository = self._repo(ws_id)
        entry = self._entry(repository, record["tree"], path)
        if entry is None or entry[1] != "blob" or entry[0] not in _REGULAR_MODES:
            raise ValidationError(f"No such regular file in the workspace: {path}", {"stage": "path"})
        if (entry[3] or 0) > SYMBOL_MAX_BYTES:
            raise ValidationError("The file is too large for symbol extraction; read it by range instead.")
        data = repository.git("cat-file", "blob", entry[2])
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("The file is not UTF-8 text.") from exc
        found: list[dict[str, Any]] = []
        method = "unsupported"
        if path.endswith(".py"):
            method = "python_ast"
            try:
                tree = ast.parse(text, filename="<workspace>")
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                return {**self._ident(record), "path": path, "method": method, "symbols": [], "complete": False,
                        "error": safe_text(f"{type(exc).__name__}: {exc}", 160)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.append({"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                  "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno)})
        elif path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
            method = "pattern"
            declaration = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(function\*?|class|interface|type|enum|const|let)\s+([A-Za-z_$][\w$]*)")
            for number, line in enumerate(text.split("\n"), 1):
                match = declaration.match(line)
                if match and (match[1] != "const" and match[1] != "let" or "=>" in line or "function" in line):
                    found.append({"name": match[2], "kind": {"const": "function", "let": "function"}.get(match[1], match[1].rstrip("*")),
                                  "line": number})
        found.sort(key=lambda s: s["line"])
        return {**self._ident(record), "path": path, "method": method, "symbols": found[:300], "symbols_total": len(found),
                "complete": method == "python_ast" and len(found) <= 300}

    # --- diff ------------------------------------------------------------------------------
    def _base_tree(self, record: dict[str, Any], against: str) -> str:
        if against == "source":
            return record["source_tree"]
        if against in record["checkpoints"]:
            return record["checkpoints"][against]["tree"]
        raise ValidationError(f"Unknown diff base '{against}'. Use 'source' or a checkpoint name.",
                              {"checkpoints": sorted(record["checkpoints"])})

    def canonical_patch(self, ws_id: str, base_tree: str, tree: str) -> bytes:
        """The complete, deterministic patch from base to tree: full object ids, no renames, no external drivers."""
        repository = self._repo(ws_id)
        return repository.git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                              "--src-prefix=a/", "--dst-prefix=b/", "-U3", base_tree, tree)

    def diff(self, ws_id: str, owner: str, *, against: str = "source", path: str = "", max_lines: int = 400,
             offset: int = 0) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="diff")
        base = self._base_tree(record, against)
        if path:
            validate_path(path)
        repository = self._repo(ws_id)
        if base == record["tree"]:
            return {**self._ident(record), "against": against, "base_tree": base, "changed": False, "files": [], "patch": "",
                    "patch_sha256": hashlib.sha256(b"").hexdigest(), "patch_bytes": 0, "complete": True}
        files = self._parse_stat(repository, base, record["tree"])
        full = self.canonical_patch(ws_id, base, record["tree"])
        patch = full if not path else repository.git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                                                       base, record["tree"], "--", path)
        text = patch.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        window = min(max(int(max_lines), 1), 5000)
        chunk = lines[offset:offset + window]
        return {**self._ident(record), "against": against, "base_tree": base, "changed": True, "files": files,
                "files_changed": len(files), "added_lines": sum(f["added"] or 0 for f in files), "removed_lines": sum(f["removed"] or 0 for f in files),
                "patch": "\n".join(chunk) + ("\n" if chunk else ""), "patch_lines_total": len(lines), "patch_offset": offset,
                "patch_lines_returned": len(chunk), "patch_has_more": offset + window < len(lines),
                "patch_sha256": hashlib.sha256(full).hexdigest(), "patch_bytes": len(full), "path_filter": path or None,
                "complete": offset == 0 and offset + window >= len(lines) and not path}

    @staticmethod
    def _parse_stat(repository: WorkspaceRepo, base: str, tree: str) -> list[dict[str, Any]]:
        names = repository.git("diff", "--no-renames", "--name-status", "-z", base, tree).split(b"\0")
        stats = repository.git("diff", "--no-renames", "--numstat", "-z", base, tree).split(b"\0")
        added: dict[str, tuple[int | None, int | None]] = {}
        for item in filter(None, stats):
            a, r, name = item.decode("utf-8", errors="replace").split("\t", 2)
            added[name] = (None if a == "-" else int(a), None if r == "-" else int(r))
        files = []
        for code, name in zip(names[0::2], names[1::2]):
            if not code:
                break
            path = name.decode("utf-8", errors="replace")
            a, r = added.get(path, (None, None))
            files.append({"path": path, "status": {"A": "added", "M": "modified", "D": "deleted"}.get(code.decode()[:1], code.decode()),
                          "added": a, "removed": r, "binary": a is None})
        return files

    # --- mutation --------------------------------------------------------------------------
    def edit(self, ws_id: str, owner: str, *, expected_version: int, operation: str, mutate: Callable[[WorkspaceRepo, dict[str, Any]], tuple[str, list[str]]]) -> dict[str, Any]:
        """Run one mutation atomically. `mutate` receives the repo (index loaded with the current tree) and returns (new_tree, paths)."""
        self.get(ws_id, owner, operation="edit")
        with self._exclusive(ws_id):
            record = self._load(ws_id)
            if record is None or record["expires_at"] <= self.clock():
                raise ValidationError("Unknown workspace_id.")
            if type(expected_version) is not int or expected_version != record["version"]:
                raise VersionConflictError(expected_version, record["version"], record["tree"])
            repository = self._repo(ws_id)
            repository.git("read-tree", record["tree"])
            new_tree, paths = mutate(repository, record)
            validate_sha(new_tree)
            if new_tree == record["tree"]:
                return {**self.summary(record), "changed": False, "operation": operation, "paths": paths}
            self._enforce_limits(repository, record, new_tree)
            record["version"] += 1
            record["tree"] = new_tree
            record["updated_at"] = self.clock()
            record["history"] = (record["history"] + [{"version": record["version"], "operation": operation,
                                                         "paths": paths[:10], "tree": new_tree, "at": record["updated_at"]}])[-MAX_HISTORY:]
            repository.git("update-ref", "refs/odytor/current", new_tree)
            record["bytes"] = self._repo_bytes(repository)
            self._save(record)
            return {**self.summary(record), "changed": True, "operation": operation, "paths": paths}

    def _enforce_limits(self, repository: WorkspaceRepo, record: dict[str, Any], new_tree: str) -> None:
        changed = repository.git("diff", "--no-renames", "--name-only", "-z", record["source_tree"], new_tree).count(b"\0")
        if changed > self.cfg.max_changed_files:
            raise ValidationError(f"The workspace would differ from its source in {changed} files; the limit is {self.cfg.max_changed_files}.",
                                  {"stage": "limits"})
        if self._repo_bytes(repository) > self.cfg.max_workspace_bytes:
            raise ValidationError("The workspace storage limit would be exceeded.", {"stage": "limits"})

    def _blob_text(self, repository: WorkspaceRepo, path: str, entry: tuple[str, str, str, int | None]) -> str:
        if entry[1] != "blob" or entry[0] not in _REGULAR_MODES:
            raise ValidationError(f"{path} is not a regular file; only regular files can be edited.", {"stage": "path"})
        if (entry[3] or 0) > self.cfg.max_file_bytes:
            raise ValidationError(f"{path} is larger than the {self.cfg.max_file_bytes}-byte edit limit.", {"stage": "limits"})
        data = repository.git("cat-file", "blob", entry[2])
        if b"\0" in data:
            raise ValidationError(f"{path} is binary.", {"stage": "content"})
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{path} is not UTF-8 text.", {"stage": "content"}) from exc

    def _check_content(self, path: str, content: Any) -> bytes:
        if not isinstance(content, str):
            raise ValidationError("content must be a string.", {"stage": "content"})
        if "\0" in content:
            raise ValidationError(f"{path}: content contains NUL; binary files cannot be written.", {"stage": "content"})
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError(f"{path}: content must be valid UTF-8.", {"stage": "content"}) from exc
        if len(data) > self.cfg.max_file_bytes:
            raise ValidationError(f"{path}: content exceeds the {self.cfg.max_file_bytes}-byte file limit.", {"stage": "limits"})
        return data

    def write_file(self, ws_id: str, owner: str, *, expected_version: int, path: str, content: str, create: bool = True) -> dict[str, Any]:
        path = _write_path(path)
        data = self._check_content(path, content)

        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            entry = self._entry(repository, record["tree"], path)
            if entry is None:
                if not create:
                    raise ValidationError(f"{path} does not exist and create=false.", {"stage": "path"})
                self._require_parents_are_directories(repository, record["tree"], path)
                mode = "100644"
            else:
                if entry[1] != "blob" or entry[0] not in _REGULAR_MODES:
                    raise ValidationError(f"{path} exists and is not a regular file.", {"stage": "path"})
                mode = entry[0]
            oid = repository.git("hash-object", "-w", "--no-filters", "--stdin", input=data).decode().strip()
            repository.git("update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")
            return repository.git("write-tree").decode().strip(), [path]
        return self.edit(ws_id, owner, expected_version=expected_version, operation="write_file", mutate=mutate)

    def replace_text(self, ws_id: str, owner: str, *, expected_version: int, path: str, old: str, new: str,
                     expected_occurrences: int = 1) -> dict[str, Any]:
        path = _write_path(path)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValidationError("old must be a non-empty string and new a string.", {"stage": "content"})

        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            entry = self._entry(repository, record["tree"], path)
            if entry is None:
                raise ValidationError(f"No such file in the workspace: {path}", {"stage": "path"})
            text = self._blob_text(repository, path, entry)
            count = text.count(old)
            if count != expected_occurrences:
                where = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)][:5]
                raise ValidationError(f"Expected {expected_occurrences} occurrence(s) of the text in {path} but found {count}. "
                                      "Include more surrounding context to make it unique, or set expected_occurrences.",
                                      {"stage": "match", "found": count, "expected": expected_occurrences, "at_lines": where})
            data = self._check_content(path, text.replace(old, new))
            oid = repository.git("hash-object", "-w", "--no-filters", "--stdin", input=data).decode().strip()
            repository.git("update-index", "--cacheinfo", f"{entry[0]},{oid},{path}")
            return repository.git("write-tree").decode().strip(), [path]
        return self.edit(ws_id, owner, expected_version=expected_version, operation="replace_text", mutate=mutate)

    def delete_path(self, ws_id: str, owner: str, *, expected_version: int, path: str, recursive: bool = False) -> dict[str, Any]:
        path = _write_path(path)

        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            entry = self._entry(repository, record["tree"], path)
            if entry is None:
                raise ValidationError(f"No such path in the workspace: {path}", {"stage": "path"})
            if entry[1] == "tree":
                if not recursive:
                    raise ValidationError(f"{path} is a directory; set recursive=true to delete it.", {"stage": "path"})
                names = [n.decode("utf-8", errors="replace") for n in repository.git("ls-tree", "-r", "-z", "--name-only", record["tree"], "--", path + "/").split(b"\0") if n]
                if len(names) > 1000:
                    raise ValidationError("Refusing to delete more than 1000 files at once.", {"stage": "limits", "files": len(names)})
            else:
                names = [path]
            for name in names:
                repository.git("update-index", "--force-remove", "--", name)
            return repository.git("write-tree").decode().strip(), names[:50]
        return self.edit(ws_id, owner, expected_version=expected_version, operation="delete", mutate=mutate)

    def rename_file(self, ws_id: str, owner: str, *, expected_version: int, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        source, destination = _write_path(source), _write_path(destination)
        if source == destination:
            raise ValidationError("source and destination are the same path.", {"stage": "path"})

        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            entry = self._entry(repository, record["tree"], source)
            if entry is None or entry[1] != "blob" or entry[0] not in _REGULAR_MODES:
                raise ValidationError(f"{source} is not an existing regular file (directories cannot be renamed).", {"stage": "path"})
            if self._entry(repository, record["tree"], destination) is not None and not overwrite:
                raise ValidationError(f"{destination} already exists; set overwrite=true to replace it.", {"stage": "path"})
            self._require_parents_are_directories(repository, record["tree"], destination)
            repository.git("update-index", "--force-remove", "--", source)
            repository.git("update-index", "--add", "--cacheinfo", f"{entry[0]},{entry[2]},{destination}")
            return repository.git("write-tree").decode().strip(), [source, destination]
        return self.edit(ws_id, owner, expected_version=expected_version, operation="rename", mutate=mutate)

    def apply_patch(self, ws_id: str, owner: str, *, expected_version: int, patch: str) -> dict[str, Any]:
        summary = parse_patch(patch, check_path=_write_path, max_bytes=self.cfg.max_file_bytes * 2, allow_delete=True,
                              modes=("100644", "100755"), max_files=self.cfg.max_changed_files, max_changed_lines=MAX_CHANGED_LINES * 5)

        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            tree = record["tree"]
            for path in summary.files:
                entry = self._entry(repository, tree, path)
                if path in summary.new_files:
                    if entry is not None:
                        raise ValidationError(f"The patch creates {path}, which already exists.", {"stage": "apply", "path": path})
                    self._require_parents_are_directories(repository, tree, path)
                elif entry is None or entry[1] != "blob" or entry[0] not in _REGULAR_MODES:
                    raise ValidationError(f"The patch targets {path}, which is not an existing regular file.", {"stage": "apply", "path": path})
            data = summary.canonical.encode("utf-8")
            try:
                repository.git("apply", "--cached", "--check", "--whitespace=nowarn", "-", input=data)
                repository.git("apply", "--cached", "--whitespace=nowarn", "-", input=data)
            except GitOperationError as exc:
                raise ValidationError(f"The patch does not apply to workspace version {record['version']}: {exc.message}",
                                      {"stage": "apply", "git_message": safe_text(exc.message, 600),
                                       "hint": "Context lines must match the current workspace content exactly; read the file and regenerate the diff."}) from exc
            raw = repository.git("diff", "--cached", "--raw", "-z", "--no-renames", tree).split(b"\0")
            changed: dict[str, str] = {}
            for meta, name in zip(raw[0::2], raw[1::2]):
                if not meta:
                    break
                fields = meta.decode().lstrip(":").split()
                status = fields[4][0]
                if status not in "AMD" or (status != "D" and fields[1] not in _REGULAR_MODES) or fields[0] not in {"000000", *_REGULAR_MODES}:
                    raise SecurityError("The patch produced a change other than a regular-file add, modify or delete.", {"stage": "apply"})
                changed[name.decode("utf-8", errors="replace")] = status
            if set(changed) != set(summary.files):
                raise SecurityError("Applied paths do not match the validated patch.", {"stage": "apply"})
            for path in summary.files:
                if path in summary.deleted_files:
                    continue
                blob = repository.git("show", ":" + path)
                if len(blob) > self.cfg.max_file_bytes or b"\0" in blob:
                    raise ValidationError(f"The result for {path} is binary or too large.", {"stage": "content"})
                try:
                    blob.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValidationError(f"The result for {path} is not UTF-8 text.", {"stage": "content"}) from exc
            return repository.git("write-tree").decode().strip(), list(summary.files)
        return self.edit(ws_id, owner, expected_version=expected_version, operation="apply_patch", mutate=mutate)

    def reset(self, ws_id: str, owner: str, *, expected_version: int, to: str = "source") -> dict[str, Any]:
        """Return the working tree to the source revision or a checkpoint. History is kept; the version still advances."""
        def mutate(repository: WorkspaceRepo, record: dict[str, Any]):
            return self._base_tree(record, to), []
        return self.edit(ws_id, owner, expected_version=expected_version, operation=f"reset:{to}", mutate=mutate)

    def checkpoint(self, ws_id: str, owner: str, *, expected_version: int, name: str) -> dict[str, Any]:
        record = self.get(ws_id, owner, operation="edit")
        if not isinstance(name, str) or not CHECKPOINT_NAME.fullmatch(name):
            raise ValidationError("Checkpoint names are 1-40 characters: lowercase letters, digits, '.', '_' or '-'.")
        if name == "source":
            raise ValidationError("'source' is reserved.")
        with self._exclusive(ws_id):
            record = self._load(ws_id) or record
            if expected_version != record["version"]:
                raise VersionConflictError(expected_version, record["version"], record["tree"])
            if name not in record["checkpoints"] and len(record["checkpoints"]) >= self.cfg.max_checkpoints:
                raise ValidationError(f"At most {self.cfg.max_checkpoints} checkpoints per workspace.")
            record["checkpoints"][name] = {"tree": record["tree"], "version": record["version"], "created_at": self.clock()}
            self._repo(ws_id).git("update-ref", f"refs/odytor/cp/{name}", record["tree"])
            self._save(record)
        return {**self.summary(record), "checkpoint": name}

    # --- execution support -----------------------------------------------------------------
    def snapshot(self, ws_id: str, owner: str, *, expected_version: int, destination: Path, max_bytes: int,
                 attach_run: str | None = None) -> dict[str, Any]:
        """Export the current tree as an immutable tar while holding the lock, so a run can never see a newer tree."""
        self.get(ws_id, owner, operation="execute")
        with self._exclusive(ws_id):
            record = self._load(ws_id)
            if record is None or record["expires_at"] <= self.clock():
                raise ValidationError("Unknown workspace_id.")
            if type(expected_version) is not int or expected_version != record["version"]:
                raise VersionConflictError(expected_version, record["version"], record["tree"])
            artifact = self._repo(ws_id).export_tree(record["tree"], destination, max_bytes=max_bytes)
            if attach_run:
                record["runs"] = (record["runs"] + [attach_run])[-100:]
                self._save(record)
            return {"record": record, "artifact": artifact}

    def attach_run(self, ws_id: str, run_id: str) -> None:
        with self._exclusive(ws_id):
            record = self._load(ws_id)
            if record is not None:
                record["runs"] = (record["runs"] + [run_id])[-100:]
                self._save(record)


# --- creation from GitHub ------------------------------------------------------------------------------------

SOURCES = ("pr_head", "prospective_merge", "branch")


def open_workspace(client: Any, repo: str, *, service: WorkspaceService, owner: str, source: str = "pr_head",
                   number: int | None = None, expected_head: str | None = None, expected_base: str | None = None,
                   branch: str | None = None, expected_sha: str | None = None,
                   operations: list[str] | None = None) -> dict[str, Any]:
    """Create a workspace at an exact revision. Every revision is verified against what was actually fetched."""
    from odytor.errors import RevisionMismatchError
    from odytor.git_workspace import pr_context, validate_ref, verify_context
    from odytor.github import validate_repo
    from odytor.pr_conflicts import inspect_merge
    from odytor.run_tests import _require_pull_access

    validate_repo(repo)
    if source not in SOURCES:
        raise ValidationError(f"source must be one of: {', '.join(SOURCES)}.")
    if source == "branch":
        if number is not None or expected_head or expected_base or not branch or not expected_sha:
            raise ValidationError("source='branch' takes branch and expected_sha only.")
        validate_ref(branch)
        validate_sha(expected_sha, "expected_sha")
    else:
        if branch or expected_sha:
            raise ValidationError("branch and expected_sha are only valid with source='branch'.")
        if type(number) is not int or number <= 0 or not expected_head:
            raise ValidationError("PR sources require number and expected_head.")
        validate_sha(expected_head, "expected_head")
        if (source == "prospective_merge") != (expected_base is not None):
            raise ValidationError("expected_base (the live target-branch tip) is required for, and only valid with, source='prospective_merge'.")
        if expected_base is not None:
            validate_sha(expected_base, "expected_base")
    _require_pull_access(client, repo)
    context = pr_context(client, repo, number, expected_head) if source != "branch" else None
    if context and (context["state"] != "open" or context["merged"]):
        raise ValidationError("The pull request must be open to open a workspace on it.")
    descriptor: dict[str, Any] = {"kind": source}

    def populate(repository: WorkspaceRepo) -> str:
        repository.client = client  # credentials are passed per command through the environment, never stored
        if source == "branch":
            repository.fetch(repo, branch, expected_sha, "head", depth=1)
            descriptor.update(branch=branch, head_sha=expected_sha, head_repo=repo)
            return repository.git("rev-parse", f"{expected_sha}^{{tree}}").decode().strip()
        depth = 1 if source == "pr_head" else None
        repository.fetch(context["head_repo"], context["head_ref"], context["head_sha"], "head", depth=depth)
        descriptor.update(head_sha=context["head_sha"], head_repo=context["head_repo"], head_ref=context["head_ref"],
                          base_ref=context["base_ref"], pr_api_base_snapshot=context["base_sha"])
        head_tree = repository.git("rev-parse", f"{context['head_sha']}^{{tree}}").decode().strip()
        descriptor["head_tree"] = head_tree
        if source == "pr_head":
            tree = head_tree
        else:
            repository.fetch(context["base_repo"], context["base_ref"], expected_base, "base")
            merge = inspect_merge(repository, expected_base, context["head_sha"], max_paths=20)
            if merge["conflicting"]:
                raise ValidationError("The exact head and base conflict, so there is no merged tree to open.",
                                      {"stage": "merge_conflict", "total_conflicting_paths": merge["total_conflicting_paths"],
                                       "paths": [c["path"] for c in merge["conflicts"]],
                                       "hint": "Inspect them with pr_conflicts(merge_target='live_tip')."})
            descriptor.update(base_sha=expected_base, base_sha_role="live_target_branch_tip", merged_tree=merge["merge_tree_sha"])
            tree = merge["merge_tree_sha"]
        verify_context(client, repo, number, context)
        return tree

    record = service.create(owner=owner, repo=repo, source=descriptor, populate=populate, pr=number,
                            operations=tuple(operations) if operations else None)
    return service.summary(record)
