"""Immutable, atomically prepared dependency environments keyed by trusted inputs (runner side).

Environments are built only from a maintainer-approved pinned specification, never from a
contributor branch, and are mounted read-only into test jobs. Standard library only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

READY = ".gp-ready"
PREP_LOCK_WAIT = 1200.0


@dataclass(frozen=True)
class EnvIdentity:
    repo: str
    requirements_sha256: str
    python: str
    image_id: str
    profile_version: str
    platform: str

    def key(self) -> str:
        material = json.dumps([self.repo.casefold(), self.requirements_sha256, self.python, self.image_id,
                               self.profile_version, self.platform], separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()[:40]


@dataclass(frozen=True)
class EnvHandle:
    key: str
    path: Path
    cache_status: str          # "hit" | "miss" | "hit_after_wait"
    prepare_seconds: float     # time this run spent preparing; zero for a reuse
    prepared_at: float
    size_bytes: int
    original_prepare_seconds: float


class EnvPreparationError(Exception):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def is_ready(path: Path, identity: EnvIdentity) -> dict | None:
    """Return the ready record only for a complete environment whose identity still matches."""
    try:
        meta = json.loads((path / READY).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("key") != identity.key() or not (path / "site").is_dir():
        return None
    return meta


@contextlib.contextmanager
def _flock(path: Path, *, exclusive: bool, wait: float | None) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, flags | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() > deadline:
                    raise EnvPreparationError("Timed out waiting for a concurrent environment preparation.")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def acquire(root: Path, identity: EnvIdentity, prepare: Callable[[Path], dict]) -> Iterator[EnvHandle]:
    """Yield a ready environment, holding a shared use lock so eviction cannot remove it.

    `prepare(tmp)` populates `tmp/site`; failures leave nothing behind. The final directory
    appears via one atomic rename of a fully built tree, so a keyed path is either absent or complete.
    """
    envs = root / "envs"
    envs.mkdir(parents=True, exist_ok=True)
    key = identity.key()
    final = envs / key
    with _flock(envs / f"{key}.use", exclusive=False, wait=None):
        status, seconds = "hit", 0.0
        meta = is_ready(final, identity)
        if meta is None:
            with _flock(envs / f"{key}.prep", exclusive=True, wait=PREP_LOCK_WAIT):
                meta = is_ready(final, identity)
                if meta is not None:
                    status = "hit_after_wait"
                else:
                    status = "miss"
                    if final.exists():  # present but incomplete or mismatched: never reuse
                        shutil.rmtree(final, ignore_errors=True)
                    tmp = envs / f".prep-{key}"
                    shutil.rmtree(tmp, ignore_errors=True)  # stale leftover; we hold the preparation lock
                    tmp.mkdir(mode=0o755)
                    os.chmod(tmp, 0o755)  # the unprivileged test user must traverse the mount root
                    (tmp / "site").mkdir(mode=0o755)
                    os.chmod(tmp / "site", 0o755)
                    started = time.monotonic()
                    try:
                        detail = prepare(tmp)
                        seconds = round(time.monotonic() - started, 3)
                        meta = {"key": key, "prepared_at": time.time(), "prepare_seconds": seconds,
                                "size_bytes": tree_size(tmp), "identity": identity.__dict__, "detail": detail}
                        (tmp / READY).write_text(json.dumps(meta))
                        os.replace(tmp, final)
                    except BaseException:
                        shutil.rmtree(tmp, ignore_errors=True)
                        raise
        with contextlib.suppress(OSError):
            os.utime(envs / f"{key}.use")
        yield EnvHandle(key, final, status, seconds, float(meta.get("prepared_at", 0.0)),
                        int(meta.get("size_bytes", 0)), float(meta.get("prepare_seconds", 0.0)))


class ResourceUnavailable(Exception):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _try_lock(path: Path, *, exclusive: bool) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def bump_stats(root: Path, **increments: int) -> dict:
    """Persistent hit/miss/eviction counters, updated under a lock."""
    root.mkdir(parents=True, exist_ok=True)
    with _flock(root / "stats.lock", exclusive=True, wait=30):
        path = root / "stats.json"
        try:
            stats = json.loads(path.read_text())
        except (OSError, ValueError):
            stats = {}
        for name, amount in increments.items():
            stats[name] = int(stats.get(name, 0)) + amount
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats))
        os.replace(tmp, path)
    return stats


def inventory(root: Path) -> list[dict]:
    """Ready environments with size and last use; anything else is not an environment."""
    envs = root / "envs"
    found = []
    for entry in envs.iterdir() if envs.is_dir() else ():
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        try:
            meta = json.loads((entry / READY).read_text())
            used = (envs / f"{entry.name}.use").stat().st_mtime
        except (OSError, ValueError):
            continue
        found.append({"key": entry.name, "size": int(meta.get("size_bytes", 0)), "last_used": used})
    return sorted(found, key=lambda e: e["last_used"])


def _evict(envs: Path, key: str) -> bool:
    """Remove one environment unless a job holds it. Rename first so it vanishes atomically."""
    fd = _try_lock(envs / f"{key}.use", exclusive=True)
    if fd is None:
        return False
    try:
        victim = envs / f".evict-{key}-{os.getpid()}"
        os.replace(envs / key, victim)
        shutil.rmtree(victim, ignore_errors=True)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def sweep_debris(root: Path, *, now: float, grace: float = 300.0) -> int:
    """Remove interrupted preparations, half-evicted trees, incomplete environments and dead job workspaces."""
    envs, removed = root / "envs", 0
    for entry in list(envs.iterdir()) if envs.is_dir() else ():
        if not entry.is_dir() or now - entry.stat().st_mtime < grace:
            continue
        name = entry.name
        if name.startswith(".evict-"):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        elif name.startswith(".prep-"):
            fd = _try_lock(envs / f"{name[6:]}.prep", exclusive=True)  # a live preparation holds this
            if fd is not None:
                shutil.rmtree(entry, ignore_errors=True)
                os.close(fd)
                removed += 1
        elif not name.startswith(".") and not (entry / READY).exists():
            fd = _try_lock(envs / f"{name}.use", exclusive=True)
            if fd is not None:
                shutil.rmtree(entry, ignore_errors=True)
                os.close(fd)
                removed += 1
    jobs = root / "jobs"
    for entry in list(jobs.iterdir()) if jobs.is_dir() else ():
        if not entry.is_dir() or now - entry.stat().st_mtime < grace:
            continue
        fd = _try_lock(entry / ".lock", exclusive=True) if (entry / ".lock").exists() else -1
        if fd is not None:  # no lock file, or nobody holds it: the owner is gone
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            if fd >= 0:
                os.close(fd)
    return removed


def collect(root: Path, *, ttl_seconds: float, budget_bytes: int, min_free_bytes: int,
            now: float | None = None, debris_grace: float = 300.0, keep: frozenset[str] = frozenset()) -> dict:
    """Evict expired, then least recently used, environments until within budget and free-space floor.

    Environments held by a running job, and those named in `keep` (the one the incoming job needs), are
    skipped. Raises ResourceUnavailable if free space
    cannot be restored, so a new job is rejected rather than run on a full disk.
    """
    now = time.time() if now is None else now
    root.mkdir(parents=True, exist_ok=True)
    (root / "envs").mkdir(exist_ok=True)
    debris = sweep_debris(root, now=now, grace=debris_grace)
    entries = inventory(root)
    total = sum(e["size"] for e in entries)
    free_before = shutil.disk_usage(root).free
    report = {"env_bytes_before": total, "env_count_before": len(entries), "free_bytes_before": free_before,
              "evicted": [], "protected": [], "debris_removed": debris}
    envs = root / "envs"

    def evict(entry: dict, reason: str) -> None:
        nonlocal total
        if _evict(envs, entry["key"]):
            total -= entry["size"]
            report["evicted"].append({"key": entry["key"], "reason": reason, "bytes": entry["size"]})
        else:
            report["protected"].append(entry["key"])

    for entry in entries:
        if entry["key"] not in keep and now - entry["last_used"] > ttl_seconds:
            evict(entry, "ttl")
    for entry in [e for e in entries if e["key"] not in {x["key"] for x in report["evicted"]}
                  and e["key"] not in report["protected"] and e["key"] not in keep]:
        if total <= budget_bytes and shutil.disk_usage(root).free >= min_free_bytes:
            break
        evict(entry, "disk_budget" if total > budget_bytes else "free_space")
    after = inventory(root)
    free_after = shutil.disk_usage(root).free
    report.update(env_bytes_after=sum(e["size"] for e in after), env_count_after=len(after), free_bytes_after=free_after)
    if report["evicted"]:
        bump_stats(root, evictions=len(report["evicted"]))
    if free_after < min_free_bytes:
        raise ResourceUnavailable("Insufficient free disk space on the runner after evicting unused environments.", report)
    return report
