"""Runner: accepts one validated job on stdin, executes it in a rootless container, returns JSON.

It is deployed on a dedicated runner host that holds no GitHub credentials. It exposes no
shell or arbitrary command: only profile-defined pytest invocations over a source artifact.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from git_plus import dep_cache
from git_plus.procs import run_bounded
from git_plus.profile import (ENV_DIR, INPUT_DIR, PROFILE_NAME, PROFILE_VERSION, WORK_DIR, ProfileError,
                              build_argv, is_job_id, is_repo, requirements_digest, validate_requirements,
                              validate_selectors)

SCHEMA = 1
MARK = "GIT_PLUS_HARNESS_RESULT:"
MAX_HEADER_BYTES = 262144
MAX_REPORT_BYTES = 4 * 1024 * 1024
_PIN_DIGEST = re.compile(r"[^@\s]+@sha256:([0-9a-f]{64})")
_PIN_ID = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_MIB = 1024 * 1024


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
            prep_timeout=number("PREP_TIMEOUT", 900, 30, 7200),
            pip_index_url=env.get("GIT_PLUS_RUNNER_PIP_INDEX_URL") or None,
            env_ttl_days=number("ENV_TTL_DAYS", 7.0, 0.01, 365.0, float),
            env_budget_bytes=number("ENV_BUDGET_GIB", 10, 1, 4096) * 1024 * _MIB,
            min_free_bytes=number("MIN_FREE_GIB", 2, 1, 4096) * 1024 * _MIB)


@dataclass(frozen=True)
class ImageInfo:
    ref: str
    image_id: str
    digest: str | None
    platform: str
    python: str


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


def _container_args(cfg: RunnerConfig, name: str, image: ImageInfo, *, test: bool, mounts: list[str],
                    timeout: int, memory_mib: int, tmpfs: list[str], env: dict[str, str], command: list[str]) -> list[str]:
    args = [cfg.engine, "run", "--rm", "--name", name, "--pull=never", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", f"--pids-limit={cfg.pids}", f"--memory={memory_mib}m",
            f"--memory-swap={memory_mib}m", f"--cpus={cfg.cpus}", "--ulimit=nofile=4096:4096", "--ulimit=core=0",
            f"--timeout={timeout}", "--entrypoint", "python", "--workdir", WORK_DIR if test else "/tmp"]
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
        name = f"gp-prep-{job_id[5:21]}"
        args = _container_args(cfg, name, image, test=False, mounts=[f"{prep}:{INPUT_DIR}:ro", f"{tmp}:{ENV_DIR}:rw"],
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


def _read_exact(stream, count: int) -> bytes:
    data = stream.read(count)
    if data is None or len(data) != count:
        raise SetupError("frame", "Truncated job frame.")
    return data


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
            chunk = stream.read(min(remaining, _MIB))
            if not chunk:
                raise SetupError("frame", "Truncated source artifact.")
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
        timeout = header.get("timeout_seconds")
        if type(timeout) is not int or not 1 <= timeout <= cfg.max_timeout:
            raise ProfileError("Timeout is outside the runner limit.")
    except ProfileError as exc:
        raise SetupError("validation", str(exc)) from exc
    return {"job_id": header["job_id"], "repo": header["repo"], "selectors": selectors,
            "requirements": requirements, "timeout": timeout}


def execute(cfg: RunnerConfig, job: dict, source: Path, job_dir: Path) -> dict:
    image = resolve_image(cfg)
    spec_sha = requirements_digest(job["requirements"])
    identity = dep_cache.EnvIdentity(job["repo"], spec_sha, image.python, image.image_id, PROFILE_VERSION, image.platform)
    result: dict = {"schema": SCHEMA, "job_id": job["job_id"],
                    "runner": {"image": image.ref, "image_id": image.image_id, "image_digest": image.digest,
                               "platform": image.platform, "python": image.python, "engine": cfg.engine}}
    try:
        with dep_cache.acquire(cfg.root, identity, _prepare(cfg, job["job_id"], image, job["requirements"], job_dir)) as env:
            result["environment"] = {"cache_key": env.key, "cache_status": env.cache_status, "requirements_sha256": spec_sha,
                                     "prepare_seconds": env.prepare_seconds, "prepared_at": env.prepared_at,
                                     "size_bytes": env.size_bytes}
            run = job_dir / "run"
            run.mkdir(mode=0o755)
            os.chmod(run, 0o755)
            os.replace(source, run / "source.tar")
            os.chmod(run / "source.tar", 0o644)
            argv = build_argv(job["selectors"])
            _write(run / "job.json", json.dumps({"argv": argv, "selectors": list(job["selectors"]), "timeout": job["timeout"],
                                                 "limits": {"files": 200000, "bytes": cfg.work_mib * _MIB}}))
            for name in ("harness.py", "pytest_report.py"):
                _write(run / name, Path(__file__).with_name(name).read_bytes())
            name = f"gp-run-{job['job_id'][5:21]}"
            args = _container_args(
                cfg, name, image, test=True, mounts=[f"{run}:{INPUT_DIR}:ro", f"{env.path}:{ENV_DIR}:ro"],
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
    except dep_cache.EnvPreparationError as exc:
        raise SetupError("prepare", exc.args[0], exc.detail) from exc
    return result


def _handler(_signum, _frame):
    raise Interrupted()


def main() -> int:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _handler)
    job_dir: Path | None = None
    try:
        try:
            cfg = RunnerConfig.load()
            (cfg.root / "jobs").mkdir(parents=True, exist_ok=True, mode=0o700)
            job_dir = cfg.root / "jobs" / f"pending-{os.getpid()}"
            job_dir.mkdir(mode=0o755)
            os.chmod(job_dir, 0o755)
            source = job_dir / "source.tar"
            job = validate_header(cfg, read_job(sys.stdin.buffer, cfg, source))
            result = execute(cfg, job, source, job_dir)
        except SetupError as exc:
            result = {"schema": SCHEMA, "setup_error": {"stage": exc.stage, "message": exc.message, "detail": exc.detail}}
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        sys.stdout.flush()
        return 0
    except Interrupted:
        return 143
    finally:
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
