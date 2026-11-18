"""Runner: accepts one validated job on stdin, executes it in a rootless container, returns JSON.

It is deployed on a dedicated runner host that holds no GitHub credentials. It exposes no
shell or arbitrary command: only profile-defined pytest invocations over a source artifact.
Standard library only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from git_plus import dep_cache
from git_plus.procs import run_bounded
from git_plus.profile import (ENV_DIR, INPUT_DIR, PROFILE_NAME, PROFILE_VERSION, WORK_DIR, ProfileError,
                              build_argv, is_job_id, is_repo, requirements_digest, validate_requirements,
                              validate_selector, validate_selectors)

SCHEMA = 1
MARK = "GIT_PLUS_HARNESS_RESULT:"
MAX_HEADER_BYTES = 262144
MAX_REPORT_BYTES = 4 * 1024 * 1024
_PIN_DIGEST = re.compile(r"[^@\s]+@sha256:([0-9a-f]{64})")
_PIN_ID = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_MIB = 1024 * 1024
LABEL_ROOT, LABEL_WORKSPACE = "git-plus.root", "git-plus.workspace"


class SetupError(Exception):
    def __init__(self, stage: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.stage, self.message, self.detail = stage, message, detail or {}


class Interrupted(BaseException):
    """SIGTERM/SIGHUP: unwind through every finally so containers and workspaces are removed."""


@dataclass(frozen=True)
class RunnerConfig:
    image: str
    root: Path
    engine: str = "podman"
    cpus: float = 2.0
    memory_mib: int = 4096
    pids: int = 512
    work_mib: int = 1024
    tmp_mib: int = 512
    max_timeout: int = 600
    max_source_bytes: int = 256 * _MIB
    prep_timeout: int = 900
    frame_timeout: int = 600
    max_queued: int = 5
    queue_wait: int = 900
    pip_index_url: str | None = None
    env_ttl_days: float = 7.0
    env_budget_bytes: int = 10 * 1024 * _MIB
    min_free_bytes: int = 2 * 1024 * _MIB

    @classmethod
    def load(cls, environ=None) -> RunnerConfig:
        env = os.environ if environ is None else environ

        def number(name: str, default, low, high, cast=int):
            raw = env.get(f"GIT_PLUS_RUNNER_{name}", "").strip()
            try:
                value = cast(raw) if raw else default
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                raise SetupError("config", f"GIT_PLUS_RUNNER_{name} must be between {low} and {high}.")
            return value

        image = env.get("GIT_PLUS_RUNNER_IMAGE", "").strip()
        if not (_PIN_DIGEST.fullmatch(image) or _PIN_ID.fullmatch(image)):
            raise SetupError("config", "GIT_PLUS_RUNNER_IMAGE must be pinned by digest (name@sha256:... or an image ID).")
        home = env.get("HOME") or "/var/lib/git-plus-runner"
        root = Path(env.get("GIT_PLUS_RUNNER_ROOT") or f"{home}/.local/state/git-plus-runner")
        return cls(
            image=image, root=root,
            cpus=number("CPUS", 2.0, 0.5, 16.0, float), memory_mib=number("MEMORY_MIB", 4096, 256, 65536),
            pids=number("PIDS", 512, 32, 8192), work_mib=number("WORK_MIB", 1024, 64, 16384),
            tmp_mib=number("TMP_MIB", 512, 16, 8192), max_timeout=number("MAX_TIMEOUT", 600, 10, 3600),
            max_source_bytes=number("MAX_SOURCE_MIB", 256, 1, 4096) * _MIB,
            prep_timeout=number("PREP_TIMEOUT", 900, 30, 7200), frame_timeout=number("FRAME_TIMEOUT", 600, 10, 3600),
            max_queued=number("MAX_QUEUED", 5, 0, 50), queue_wait=number("QUEUE_WAIT", 900, 1, 7200),
            pip_index_url=env.get("GIT_PLUS_RUNNER_PIP_INDEX_URL") or None,
            env_ttl_days=number("ENV_TTL_DAYS", 7.0, 0.01, 365.0, float),
            env_budget_bytes=number("ENV_BUDGET_MIB", 10240, 1, 4194304) * _MIB,
            min_free_bytes=number("MIN_FREE_MIB", 2048, 1, 4194304) * _MIB)


@dataclass(frozen=True)
class ImageInfo:
    ref: str
    image_id: str
    digest: str | None
    platform: str
    python: str


def load_config_file(path: str) -> dict[str, str]:
    """Read GIT_PLUS_RUNNER_* assignments from a root-owned file; anything else, or a writable file, is refused."""
    info = os.stat(path)
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SetupError("config", "The runner configuration file must be owned by root and not group/world writable.")
    values: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"GIT_PLUS_RUNNER_[A-Z_]+", key.strip()):
            raise SetupError("config", f"Invalid runner configuration line {number}.")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _runtime_config() -> RunnerConfig:
    """Process environment, overlaid by the optional root-owned file named by GIT_PLUS_RUNNER_CONFIG."""
    path = os.environ.get("GIT_PLUS_RUNNER_CONFIG")
    if not path:
        return RunnerConfig.load()
    try:
        return RunnerConfig.load({**os.environ, **load_config_file(path)})
    except OSError as exc:
        raise SetupError("config", f"Cannot read the runner configuration file: {exc.strerror}.") from exc


def _engine(cfg: RunnerConfig, *args: str, timeout: float = 60, max_stdout: int = 65536):
    env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG") if k in os.environ}
    return run_bounded([cfg.engine, *args], env=env, timeout=timeout, max_stdout=max_stdout)


def resolve_image(cfg: RunnerConfig) -> ImageInfo:
    result = _engine(cfg, "image", "inspect", "--format",
                     "{{.Id}}|{{.Digest}}|{{.Architecture}}|{{.Os}}|{{json .RepoDigests}}", cfg.image)
    if result.returncode != 0:
        raise SetupError("image", "The pinned runner image is not available locally; load it by digest.",
                         {"stderr": result.stderr_tail.decode("utf-8", "replace")[-300:]})
    image_id, digest, arch, os_name, repo_digests = result.stdout.decode().strip().split("|", 4)
    known = {digest, *[d.split("@")[-1] for d in json.loads(repo_digests or "[]")]}
    pin = _PIN_DIGEST.fullmatch(cfg.image)
    if pin:
        if f"sha256:{pin[1]}" not in known:
            raise SetupError("image", "The local image does not match the pinned digest.")
    elif _PIN_ID.fullmatch(cfg.image)[1] != image_id.removeprefix("sha256:"):
        raise SetupError("image", "The local image does not match the pinned image ID.")
    image_id = image_id.removeprefix("sha256:")
    cache = cfg.root / f"image-{image_id}.json"
    try:
        python = json.loads(cache.read_text())["python"]
    except (OSError, ValueError, KeyError):
        probe = _engine(cfg, "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "--user", "65534:65534", "--entrypoint", "python", image_id,
                        "-c", "import platform;print(platform.python_version())", timeout=60)
        python = probe.stdout.decode().strip()
        if probe.returncode != 0 or not re.fullmatch(r"3\.\d+\.\d+", python):
            raise SetupError("image", "The runner image has no usable Python interpreter.")
        cfg.root.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"python": python}))
    return ImageInfo(cfg.image, image_id, digest or None, f"{os_name}/{arch}", python)


def _write(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


def _container_args(cfg: RunnerConfig, name: str, image: ImageInfo, *, test: bool, mounts: list[str], workspace: str,
                    timeout: int, memory_mib: int, tmpfs: list[str], env: dict[str, str], command: list[str]) -> list[str]:
    args = [cfg.engine, "run", "--rm", "--name", name, "--pull=never", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", f"--pids-limit={cfg.pids}", f"--memory={memory_mib}m",
            f"--memory-swap={memory_mib}m", f"--cpus={cfg.cpus}", "--ulimit=nofile=4096:4096", "--ulimit=core=0",
            f"--timeout={timeout}", f"--label={LABEL_ROOT}={_root_tag(cfg.root)}", f"--label={LABEL_WORKSPACE}={workspace}",
            "--entrypoint", "python", "--workdir", WORK_DIR if test else "/tmp"]
    if test:
        args += ["--network=none", "--user", "65534:65534"]
    for mount in mounts:
        args += ["-v", mount]
    for spec in tmpfs:
        args += ["--tmpfs", spec]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args + [image.image_id, *command]


def _parse_report(stdout: bytes) -> dict | None:
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(MARK):
            try:
                value = json.loads(line[len(MARK):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _kill_container(cfg: RunnerConfig, name: str) -> None:
    try:
        _engine(cfg, "rm", "-f", "-t", "0", name, timeout=30)
    except Exception:
        pass


def _run_container(cfg: RunnerConfig, name: str, args: list[str], timeout: float) -> tuple[object, float]:
    started = time.monotonic()
    env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG") if k in os.environ}
    try:
        result = run_bounded(args, env=env, timeout=timeout, max_stdout=MAX_REPORT_BYTES)
    finally:
        _kill_container(cfg, name)
    return result, round(time.monotonic() - started, 3)


def _prepare(cfg: RunnerConfig, job_id: str, image: ImageInfo, requirements: str, job_dir: Path):
    def build(tmp: Path) -> dict:
        prep = job_dir / "prep"
        prep.mkdir(mode=0o755)
        os.chmod(prep, 0o755)
        _write(prep / "requirements.txt", requirements)
        _write(prep / "job.json", json.dumps({"timeout": cfg.prep_timeout}))
        _write(prep / "harness.py", Path(__file__).with_name("harness.py").read_bytes())
        env = {"PIP_INDEX_URL": cfg.pip_index_url} if cfg.pip_index_url else {}
        name = f"gp-prep-{job_dir.name[4:]}"
        args = _container_args(cfg, name, image, test=False, workspace=job_dir.name,
                               mounts=[f"{prep}:{INPUT_DIR}:ro", f"{tmp}:{ENV_DIR}:rw"],
                               timeout=cfg.prep_timeout + 60, memory_mib=cfg.memory_mib, env=env,
                               tmpfs=[f"/tmp:rw,nosuid,nodev,exec,size={cfg.tmp_mib * 2}m"],
                               command=[f"{INPUT_DIR}/harness.py", "prep"])
        result, _ = _run_container(cfg, name, args, cfg.prep_timeout + 90)
        report = _parse_report(result.stdout)
        if result.timed_out or report is None or report.get("exit") != 0:
            raise dep_cache.EnvPreparationError(
                "Dependency preparation failed.",
                {"exit": (report or {}).get("exit"), "timed_out": bool(result.timed_out or (report or {}).get("timed_out")),
                 "output_tail": str((report or {}).get("output_tail", result.stderr_tail.decode("utf-8", "replace")))[-2048:]})
        return {"python": report.get("python"), "machine": report.get("machine")}
    return build


class FdReader:
    """Blocking reads from a pipe with one overall deadline, so a stalled client cannot hold a workspace."""

    def __init__(self, fd: int, deadline_seconds: float) -> None:
        self.fd, self.deadline = fd, time.monotonic() + deadline_seconds

    def read(self, count: int) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
            raise SetupError("frame", "Timed out waiting for the job frame.")
        return os.read(self.fd, min(count, _MIB))


def _read_exact(stream, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise SetupError("frame", "Truncated job frame.")
        data += chunk
    return bytes(data)


def read_job(stream, cfg: RunnerConfig, source: Path) -> dict:
    """Frame: 4-byte header length, JSON header, 8-byte artifact length, tar artifact."""
    size = int.from_bytes(_read_exact(stream, 4), "big")
    if not 0 < size <= MAX_HEADER_BYTES:
        raise SetupError("frame", "Invalid job header size.")
    try:
        header = json.loads(_read_exact(stream, size))
    except ValueError as exc:
        raise SetupError("frame", "Job header is not valid JSON.") from exc
    length = int.from_bytes(_read_exact(stream, 8), "big")
    if length > cfg.max_source_bytes:
        raise SetupError("validation", "Source artifact exceeds the runner limit.")
    remaining = length
    with source.open("wb") as handle:
        while remaining:
            chunk = _read_exact(stream, min(remaining, _MIB))
            handle.write(chunk)
            remaining -= len(chunk)
    return header


def validate_header(cfg: RunnerConfig, header: object) -> dict:
    try:
        if not isinstance(header, dict) or header.get("schema") != SCHEMA:
            raise ProfileError("Unsupported job schema.")
        if not is_job_id(header.get("job_id")) or not is_repo(header.get("repo")):
            raise ProfileError("Invalid job or repository identifier.")
        if header.get("profile") != PROFILE_NAME or header.get("profile_version") != PROFILE_VERSION:
            raise ProfileError("Unsupported profile or profile version.")
        dirs = header.get("test_dirs")
        if not isinstance(dirs, list) or not dirs or not all(isinstance(d, str) for d in dirs):
            raise ProfileError("Invalid test directories.")
        selectors = validate_selectors(header.get("selectors"), tuple(dirs))
        requirements = validate_requirements(header.get("requirements"))
        overlay = header.get("overlay_paths", [])
        if not isinstance(overlay, list) or len(overlay) > 20:
            raise ProfileError("Invalid overlay path list.")
        overlay_paths = tuple(validate_selector(p, tuple(dirs)) for p in overlay)
        timeout = header.get("timeout_seconds")
        if type(timeout) is not int or not 1 <= timeout <= cfg.max_timeout:
            raise ProfileError("Timeout is outside the runner limit.")
    except ProfileError as exc:
        raise SetupError("validation", str(exc)) from exc
    return {"job_id": header["job_id"], "repo": header["repo"], "selectors": selectors,
            "requirements": requirements, "timeout": timeout, "overlay_paths": overlay_paths}


def _run_job(cfg: RunnerConfig, job: dict, image: ImageInfo, env: dep_cache.EnvHandle, source: Path,
             job_dir: Path, result: dict) -> None:
    run = job_dir / "run"
    run.mkdir(mode=0o755)
    os.chmod(run, 0o755)
    os.replace(source, run / "source.tar")
    os.chmod(run / "source.tar", 0o644)
    argv = build_argv(job["selectors"])
    _write(run / "job.json", json.dumps({"argv": argv, "selectors": list(job["selectors"]), "overlay_paths": list(job["overlay_paths"]),
                                         "timeout": job["timeout"],
                                         "limits": {"files": 200000, "bytes": cfg.work_mib * _MIB}}))
    for name in ("harness.py", "pytest_report.py"):
        _write(run / name, Path(__file__).with_name(name).read_bytes())
    name = f"gp-run-{job_dir.name[4:]}"
    args = _container_args(
        cfg, name, image, test=True, workspace=job_dir.name, mounts=[f"{run}:{INPUT_DIR}:ro", f"{env.path}:{ENV_DIR}:ro"],
        timeout=job["timeout"] + 60, memory_mib=cfg.memory_mib,
        tmpfs=[f"/tmp:rw,nosuid,nodev,exec,size={cfg.tmp_mib}m", f"{WORK_DIR}:rw,nosuid,nodev,exec,size={cfg.work_mib}m"],
        env={}, command=[f"{INPUT_DIR}/harness.py", "test"])
    outcome, seconds = _run_container(cfg, name, args, job["timeout"] + 90)
    result["sandbox"] = {
        "engine_exit_code": outcome.returncode, "timed_out": outcome.timed_out, "output_overflow": outcome.overflowed,
        "container_seconds": seconds, "engine_stderr_tail": outcome.stderr_tail.decode("utf-8", "replace")[-1024:],
        "limits": {"cpus": cfg.cpus, "memory_mib": cfg.memory_mib, "pids": cfg.pids, "network": "none",
                   "work_tmpfs_mib": cfg.work_mib, "tmp_tmpfs_mib": cfg.tmp_mib, "timeout_seconds": job["timeout"],
                   "user": "65534:65534", "root_filesystem": "read-only", "capabilities": "none"}}
    result["report"] = _parse_report(outcome.stdout)


def execute(cfg: RunnerConfig, job: dict, source: Path, job_dir: Path) -> dict:
    image = resolve_image(cfg)
    reap_orphan_containers(cfg)
    _sweep_active(cfg)
    spec_sha = requirements_digest(job["requirements"])
    identity = dep_cache.EnvIdentity(job["repo"], spec_sha, image.python, image.image_id, PROFILE_VERSION, image.platform)
    result: dict = {"schema": SCHEMA, "job_id": job["job_id"],
                    "runner": {"image": image.ref, "image_id": image.image_id, "image_digest": image.digest,
                               "platform": image.platform, "python": image.python, "engine": cfg.engine}}
    try:
        result["storage"] = dep_cache.collect(cfg.root, ttl_seconds=cfg.env_ttl_days * 86400,
                                              budget_bytes=cfg.env_budget_bytes, min_free_bytes=cfg.min_free_bytes,
                                              keep=frozenset({identity.key()}))
    except dep_cache.ResourceUnavailable as exc:
        raise SetupError("resources", exc.args[0], exc.detail) from exc
    try:
        with dep_cache.acquire(cfg.root, identity, _prepare(cfg, job["job_id"], image, job["requirements"], job_dir)) as env:
            stats = dep_cache.bump_stats(cfg.root, **{"misses" if env.cache_status == "miss" else "hits": 1})
            result["environment"] = {"cache_key": env.key, "cache_status": env.cache_status, "requirements_sha256": spec_sha,
                                     "prepare_seconds": env.prepare_seconds, "prepared_at": env.prepared_at,
                                     "size_bytes": env.size_bytes}
            result["cache_totals"] = {k: int(v) for k, v in stats.items()}
            _run_job(cfg, job, image, env, source, job_dir, result)
    except dep_cache.EnvPreparationError as exc:
        dep_cache.bump_stats(cfg.root, prepare_failures=1)
        raise SetupError("prepare", exc.args[0], exc.detail) from exc
    return result


def _root_tag(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def _lock_free(path: Path) -> bool:
    """True when no live runner holds this workspace lock (or the lock file is gone)."""
    fd = dep_cache._try_lock(path, exclusive=True) if path.exists() else -1
    if fd is None:
        return False
    if fd >= 0:
        os.close(fd)
    return True


def reap_orphan_containers(cfg: RunnerConfig, *, strict: bool = False) -> list[str]:
    """Remove containers whose runner is gone (SIGKILL, crash, lost SSH session) instead of waiting for their hard timeout."""
    listing = _engine(cfg, "ps", "-a", "--filter", f"label={LABEL_ROOT}={_root_tag(cfg.root)}", "--format",
                      '{{.Names}} {{index .Labels "' + LABEL_WORKSPACE + '"}}')
    if listing.returncode != 0:
        if strict:
            raise SetupError("engine", "Could not list runner containers.", {"stderr": listing.stderr_tail.decode("utf-8", "replace")[-300:]})
        return []
    reaped = []
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        name, _, workspace = line.partition(" ")
        if not re.fullmatch(r"gp-(?:run|prep)-[0-9]+-[0-9a-f]{8}", name) or not re.fullmatch(r"job-[0-9]+-[0-9a-f]{8}", workspace):
            continue
        if _lock_free(cfg.root / "jobs" / workspace / ".lock"):
            _kill_container(cfg, name)
            reaped.append(name)
    return reaped


@contextlib.contextmanager
def _single_flight(cfg: RunnerConfig, job_id: str):
    """One execution per job id: a replayed submission is rejected without touching the first one's containers."""
    active = cfg.root / "active"
    active.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = active / f"{job_id}.lock"
    fd = dep_cache._try_lock(path, exclusive=True)
    if fd is None:
        raise SetupError("duplicate", "A job with this id is already running on the runner.")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        os.close(fd)


@contextlib.contextmanager
def _admission(cfg: RunnerConfig, record: dict):
    """Host-wide admission: one job executes at a time, at most `max_queued` wait, in arrival order.

    Every runner process takes a ticket (a file it holds a lock on for its whole life). A job may run only when
    its ticket is the oldest live one, so tickets of crashed or disconnected processes drop out by themselves
    (the kernel releases their locks) and nobody starves behind them. Beyond `1 + max_queued` live tickets, or
    after `queue_wait` seconds of waiting, the job is refused with stage `busy` and nothing has run.
    """
    queue = cfg.root / "queue"
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    ticket = queue / f"{time.time_ns():020d}-{os.getpid()}-{os.urandom(3).hex()}.ticket"
    fd = os.open(ticket, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)

    def live() -> list[Path]:
        found = []
        for entry in sorted(queue.glob("*.ticket")):
            if entry == ticket:
                found.append(entry)
            elif not _lock_free(entry):
                found.append(entry)
            elif time.time() - entry.stat().st_mtime > 2:  # the grace covers a ticket being created and locked
                entry.unlink(missing_ok=True)
        return found

    started = time.monotonic()
    try:
        tickets = live()
        record["position_at_arrival"] = tickets.index(ticket)
        if len(tickets) > 1 + cfg.max_queued:
            raise SetupError("busy", "The runner queue is full; retry later.",
                             {"live_jobs": len(tickets) - 1, "max_queued": cfg.max_queued})
        while live()[0] != ticket:
            if time.monotonic() - started > cfg.queue_wait:
                raise SetupError("busy", "Timed out waiting for the runner; retry later.", {"waited_seconds": round(time.monotonic() - started)})
            time.sleep(0.25)
        record["waited_seconds"] = round(time.monotonic() - started, 3)
        yield
    finally:
        ticket.unlink(missing_ok=True)
        os.close(fd)


def _sweep_active(cfg: RunnerConfig) -> None:
    for entry in list((cfg.root / "active").glob("test-*.lock")):
        if _lock_free(entry):
            entry.unlink(missing_ok=True)


def check(cfg: RunnerConfig) -> dict:
    """Deployment self-test: rootless engine, pinned image, cgroup limits, storage and an isolated container."""
    report: dict = {"ok": False, "checks": {}}
    checks = report["checks"]
    info = _engine(cfg, "info", "--format", "{{.Host.Security.Rootless}}|{{json .Host.CgroupControllers}}|{{.Host.CgroupManager}}")
    parts = info.stdout.decode().strip().split("|", 2) if info.returncode == 0 else []
    checks["rootless"] = len(parts) == 3 and parts[0] == "true"
    controllers = set(json.loads(parts[1] or "[]")) if len(parts) == 3 else set()
    checks["cgroup_controllers"] = sorted(controllers)
    checks["cgroup_limits_delegated"] = {"cpu", "memory", "pids"} <= controllers
    image = resolve_image(cfg)
    checks["image"] = {"id": image.image_id, "digest": image.digest, "python": image.python, "platform": image.platform}
    cfg.root.mkdir(parents=True, exist_ok=True)
    checks["free_bytes"] = shutil.disk_usage(cfg.root).free
    checks["free_ok"] = checks["free_bytes"] >= cfg.min_free_bytes
    probe = _engine(cfg, "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--user", "65534:65534", f"--memory={cfg.memory_mib}m",
                    f"--cpus={cfg.cpus}", f"--pids-limit={cfg.pids}", "--entrypoint", "python", image.image_id, "-c",
                    "import os;print(os.getuid())", timeout=120)
    checks["isolated_container_runs"] = probe.returncode == 0 and probe.stdout.decode().strip() == "65534"
    if probe.returncode != 0:
        checks["isolated_container_error"] = probe.stderr_tail.decode("utf-8", "replace")[-300:]
    report["ok"] = bool(checks["rootless"] and checks["cgroup_limits_delegated"] and checks["free_ok"]
                        and checks["isolated_container_runs"])
    return report


def gc(cfg: RunnerConfig) -> dict:
    """Scheduled housekeeping: the same reaping and eviction that precede every job, for idle periods."""
    reaped = reap_orphan_containers(cfg, strict=True)
    _sweep_active(cfg)
    for stale in list((cfg.root / "queue").glob("*.ticket")):
        if _lock_free(stale) and time.time() - stale.stat().st_mtime > 2:
            stale.unlink(missing_ok=True)
    try:
        report = dep_cache.collect(cfg.root, ttl_seconds=cfg.env_ttl_days * 86400, budget_bytes=cfg.env_budget_bytes,
                                   min_free_bytes=cfg.min_free_bytes)
    except dep_cache.ResourceUnavailable as exc:
        report = {"error": exc.args[0], **exc.detail}
    return {"orphan_containers_removed": reaped, "storage": report}


def remove_workspace(job_dir: Path) -> dict:
    """Delete the disposable workspace and prove it is gone."""
    size = dep_cache.tree_size(job_dir) if job_dir.exists() else 0
    shutil.rmtree(job_dir, ignore_errors=True)
    return {"job_workspace_bytes": size, "job_workspace_removed": not job_dir.exists()}


def _handler(_signum, _frame):
    raise Interrupted()


def _watch_client(fd: int, armed: threading.Event) -> None:
    """The client keeps stdin open until it has read the result, so EOF means it is gone or gave up: unwind and clean up."""
    try:
        while os.read(fd, 65536):
            pass
    except OSError:
        pass
    if armed.is_set():
        os.kill(os.getpid(), signal.SIGTERM)


def _emit(result: dict) -> None:
    try:
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass  # the client is gone; the workspace is already removed and Git Plus records the loss


def _admin(command: str) -> int:
    try:
        cfg = _runtime_config()
        report = {"check": check, "gc": gc}[command](cfg)
    except SetupError as exc:
        report = {"ok": False, "error": {"stage": exc.stage, "message": exc.message}}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok", "error" not in report) else 1


def main() -> int:
    if sys.argv[1:]:
        if sys.argv[1:] not in (["check"], ["gc"]):
            sys.stderr.write("usage: git-plus-runner [check|gc]   (no arguments: serve one job on stdin)\n")
            return 2
        return _admin(sys.argv[1])
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _handler)
    job_dir: Path | None = None
    lock_fd = None
    armed = threading.Event()
    try:
        try:
            cfg = _runtime_config()
            (cfg.root / "jobs").mkdir(parents=True, exist_ok=True, mode=0o700)
            job_dir = cfg.root / "jobs" / f"job-{os.getpid()}-{os.urandom(4).hex()}"
            job_dir.mkdir(mode=0o755)
            os.chmod(job_dir, 0o755)
            lock_fd = os.open(job_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # a live job holds this; crash recovery sweeps unlocked workspaces
            source = job_dir / "source.tar"
            job = validate_header(cfg, read_job(FdReader(0, cfg.frame_timeout), cfg, source))
            armed.set()
            threading.Thread(target=_watch_client, args=(0, armed), daemon=True).start()
            admission: dict = {}
            with _single_flight(cfg, job["job_id"]), _admission(cfg, admission):
                result = execute(cfg, job, source, job_dir)
                result["admission"] = admission
        except SetupError as exc:
            result = {"schema": SCHEMA, "setup_error": {"stage": exc.stage, "message": exc.message, "detail": exc.detail}}
        armed.clear()
        result["cleanup"] = remove_workspace(job_dir) if job_dir else {}
        _emit(result)
        return 0
    except Interrupted:
        return 143
    finally:
        armed.clear()
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
