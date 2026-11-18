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
