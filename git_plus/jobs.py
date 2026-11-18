"""Bounded test-run queue, durable result store and evidence-based classification.

State is a small JSON file per run plus one in-process worker thread. There is no database
and no external queue. Tokens never enter a queued job: the trusted preparation (fetch, merge,
overlay, export) completes during the request and only a credential-free artifact is queued.
"""
from __future__ import annotations

import collections
import fcntl
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from git_plus.config import RunSettings
from git_plus.diagnostics import safe_text
from git_plus.errors import GitPlusError, ValidationError
from git_plus.procs import run_bounded

SCHEMA = 1
TERMINAL = frozenset({"passed", "failed", "no_tests_collected", "no_tests_executed", "setup_error",
                      "timeout", "canceled", "merge_conflict"})
_RUN_ID = re.compile(r"test-[0-9a-f]{32}")
_CACHE_STATUS = {"hit", "miss", "hit_after_wait"}
_RUNNER_PASSTHROUGH = ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
MAX_RUNNER_OUTPUT = 4 * 1024 * 1024
TEST_ENTRY_BUDGET = 30000


class QueueFullError(GitPlusError):
    """The bounded queue or spool disk cannot accept another run."""


def new_run_id() -> str:
    return "test-" + os.urandom(16).hex()


def _num(value: Any, high: float = 1e12) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= high else 0.0


def _int(value: Any, high: int = 10**9) -> int:
    return value if type(value) is int and 0 <= value <= high else 0


def _str(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _log_part(value: Any, limit: int) -> str:
    return _str(value, limit)


class RunStore:
    """One atomic JSON file per run; owner-only permissions."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValidationError("Unknown run_id.")
        return self.directory / f"{run_id}.json"

    def save(self, record: dict[str, Any]) -> None:
        target = self.path(record["run_id"])
        tmp = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, separators=(",", ":"))
        os.replace(tmp, target)

    def load(self, run_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.path(run_id).read_text())
        except (OSError, ValueError, ValidationError):  # malformed ids are simply unknown
            return None

    def records(self) -> list[dict[str, Any]]:
        found = []
        for file in self.directory.glob("test-*.json"):
            try:
                found.append(json.loads(file.read_text()))
            except (OSError, ValueError):
                continue
        return found

    def delete(self, run_id: str) -> None:
        self.path(run_id).unlink(missing_ok=True)


@dataclass
class QueuedJob:
    run_id: str
    spool: Path
    header: dict[str, Any]
    timeout: int


Runner = Callable[[QueuedJob, "threading.Event"], dict[str, Any]]


def _frame(header: dict[str, Any], artifact: Path):
    encoded = json.dumps(header, separators=(",", ":")).encode()
    yield len(encoded).to_bytes(4, "big") + encoded + artifact.stat().st_size.to_bytes(8, "big")
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            yield chunk


def runner_environment() -> dict[str, str]:
    """The runner never inherits GitHub, OAuth or service credentials from this process."""
    env = {k: os.environ[k] for k in _RUNNER_PASSTHROUGH if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("GIT_PLUS_RUNNER_")})
    return env


def make_subprocess_runner(command: tuple[str, ...], *, extra_wait: int = 1500) -> Runner:
    def run(job: QueuedJob, cancel: threading.Event) -> dict[str, Any]:
        result = run_bounded(command, env=runner_environment(), timeout=job.timeout + extra_wait,
                             max_stdout=MAX_RUNNER_OUTPUT, cancel=cancel,
                             stdin_chunks=_frame(job.header, job.spool / "source.tar"), hold_stdin=True)
        if result.canceled:
            return {"canceled": True}
        stderr = safe_text(result.stderr_tail.decode("utf-8", "replace"), 600)
        if result.timed_out:
            return {"setup_error": {"stage": "runner", "message": "The runner did not respond in time.", "detail": {"stderr": stderr}}}
        if result.overflowed or result.returncode != 0:
            return {"setup_error": {"stage": "runner", "message": "The runner failed or returned oversized output.",
                                    "detail": {"exit_code": result.returncode, "stderr": stderr}}}
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return {"setup_error": {"stage": "protocol", "message": "The runner returned invalid JSON.", "detail": {"stderr": stderr}}}
        return payload if isinstance(payload, dict) else {"setup_error": {"stage": "protocol", "message": "Invalid runner payload."}}
    return run


def _sanitize_runner(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"image": _str(raw.get("image"), 200), "image_id": _str(raw.get("image_id"), 64),
            "image_digest": _str(raw.get("image_digest"), 80) or None, "platform": _str(raw.get("platform"), 40),
            "python": _str(raw.get("python"), 20), "engine": _str(raw.get("engine"), 20)}


def _sanitize_environment(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("cache_status") if raw.get("cache_status") in _CACHE_STATUS else "unknown"
    return {"cache_key": _str(raw.get("cache_key"), 40), "cache_status": status,
            "cache_hit": status in {"hit", "hit_after_wait"},
            "requirements_sha256": _str(raw.get("requirements_sha256"), 64),
            "prepare_seconds": round(_num(raw.get("prepare_seconds"), 1e6), 3),
            "prepared_at": _num(raw.get("prepared_at")), "size_bytes": _int(raw.get("size_bytes"), 10**13)}


def _sanitize_storage(payload: dict[str, Any]) -> dict[str, Any]:
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    totals = payload.get("cache_totals") if isinstance(payload.get("cache_totals"), dict) else {}
    evicted = [{"key": _str(e.get("key"), 40), "reason": _str(e.get("reason"), 20), "bytes": _int(e.get("bytes"), 10**13)}
               for e in (storage.get("evicted") or [])[:20] if isinstance(e, dict)]
    keys = ("env_bytes_before", "env_bytes_after", "env_count_before", "env_count_after", "free_bytes_before",
            "free_bytes_after", "debris_removed")
    return {**{k: _int(storage.get(k), 10**15) for k in keys}, "evicted": evicted,
            "protected_in_use": len(storage.get("protected") or []),
            "job_workspace_bytes": _int(cleanup.get("job_workspace_bytes"), 10**13),
            "job_workspace_removed": cleanup.get("job_workspace_removed") is True,
            "cache_totals": {k: _int(v, 10**9) for k, v in totals.items() if isinstance(k, str)}}


def _sanitize_tests(raw: Any) -> list[dict[str, Any]]:
    tests, used = [], 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        entry = {"id": _str(item.get("id"), 200), "outcome": _str(item.get("outcome"), 12),
                 "duration": round(_num(item.get("duration"), 1e6), 4)}
        if item.get("message"):
            entry["message"] = _str(item.get("message"), 200)
        used += len(json.dumps(entry))
        if used > TEST_ENTRY_BUDGET:
            break
        tests.append(entry)
    return tests


_COUNT_KEYS = ("collected", "reported", "passed", "failed", "errors", "skipped", "xfailed", "xpassed",
               "incomplete", "executed", "not_run")


def classify(payload: dict[str, Any], selectors: tuple[str, ...], expected_spec_sha: str,
             overlay_paths: tuple[str, ...] = ()) -> dict[str, Any]:
    """Map untrusted runner evidence to a status; success needs affirmative proof of executed passes."""
    outcome: dict[str, Any] = {"runner": _sanitize_runner(payload.get("runner")),
                               "environment": _sanitize_environment(payload.get("environment")),
                               "failure": None, "counts": None, "tests": [], "selector_coverage": [], "log": None,
                               "execution": None, "reports": {}, "storage": _sanitize_storage(payload)}

    def end(status: str, kind: str | None = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
        outcome["status"] = status
        if kind:
            outcome["failure"] = {"kind": kind, "message": message or kind, **extra}
        return outcome

    problem = payload.get("setup_error")
    if isinstance(problem, dict):
        return end("setup_error", _str(problem.get("stage"), 40) or "runner", _str(problem.get("message"), 300),
                   detail=safe_text(json.dumps(problem.get("detail", {}), default=str), 1200))
    sandbox = payload.get("sandbox") if isinstance(payload.get("sandbox"), dict) else {}
    limits = sandbox.get("limits") if isinstance(sandbox.get("limits"), dict) else {}
    admission = payload.get("admission") if isinstance(payload.get("admission"), dict) else {}
    outcome["execution"] = {
        "runner_queue_wait_seconds": round(_num(admission.get("waited_seconds"), 1e6), 3),
        "container_seconds": round(_num(sandbox.get("container_seconds"), 1e6), 3),
        "limits": {k: (v if isinstance(v, (int, float, str)) else None) for k, v in limits.items() if isinstance(k, str)}}
    if outcome["environment"]["requirements_sha256"] != expected_spec_sha:
        return end("setup_error", "spec_mismatch", "The prepared environment does not match the approved specification.")
    report = payload.get("report")
    if sandbox.get("timed_out"):
        return end("timeout", "timeout", "The sandbox exceeded its execution deadline.")
    if not isinstance(report, dict) or report.get("harness") != 1 or report.get("kind") != "test":
        return end("setup_error", "missing_report", "The sandbox produced no valid report.",
                   engine_exit_code=sandbox.get("engine_exit_code"),
                   stderr=safe_text(_str(sandbox.get("engine_stderr_tail"), 600), 600))
    argv = report.get("argv")
    outcome["execution"]["invocation"] = [_str(a, 600) for a in argv[:80]] if isinstance(argv, list) else []
    if isinstance(report.get("error"), dict):
        err = report["error"]
        return end("setup_error", _str(err.get("stage"), 40) or "sandbox", _str(err.get("message"), 300))
    raw_counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    counts = {key: _int(raw_counts.get(key)) for key in _COUNT_KEYS}
    outcome["counts"] = counts
    outcome["tests"] = _sanitize_tests(report.get("tests"))
    outcome["tests_total"] = _int(report.get("tests_total"))
    outcome["tests_truncated"] = outcome["tests_total"] > len(outcome["tests"])
    log = report.get("log") if isinstance(report.get("log"), dict) else {}
    outcome["log"] = {"head": _log_part(log.get("head"), 4096), "tail": _log_part(log.get("tail"), 8192),
                      "total_bytes": _int(log.get("total_bytes"), 10**13), "truncated": bool(log.get("truncated"))}
    outcome["execution"].update(pytest_exit_code=report.get("pytest_exit") if type(report.get("pytest_exit")) is int else None,
                                pytest_seconds=round(_num(report.get("duration_seconds"), 1e6), 3))
    outcome["reports"] = {"collect_errors": [{"id": _str(e.get("id"), 200), "message": _str(e.get("message"), 200)}
                                              for e in (report.get("collect_errors") or [])[:20] if isinstance(e, dict)],
                          "malformed_events": _int(report.get("malformed_events")),
                          "event_ids_truncated": bool(report.get("ids_truncated"))}
    coverage = []
    for item in report.get("selector_coverage") if isinstance(report.get("selector_coverage"), list) else []:
        if isinstance(item, dict):
            coverage.append({"selector": _str(item.get("selector"), 512), "collected": _int(item.get("collected")),
                             "executed": _int(item.get("executed"))})
    outcome["selector_coverage"] = coverage
    outcome["overlay_coverage"] = [{"path": _str(i.get("selector"), 300), "collected": _int(i.get("collected")),
                                    "executed": _int(i.get("executed"))}
                                   for i in report.get("overlay_coverage", []) if isinstance(i, dict)][:20]
    if report.get("timed_out"):
        return end("timeout", "timeout", "pytest exceeded the execution deadline.")
    code, finished = outcome["execution"]["pytest_exit_code"], report.get("finished_event")
    if code is None:
        return end("setup_error", "missing_report", "No pytest exit status was reported.")
    if code < 0:
        return end("failed", "killed_by_signal", f"pytest was killed by signal {-code} (possibly a resource limit).")
    if code == 5:
        return end("no_tests_collected", "no_tests_collected", "The selectors matched no tests.")
    if code == 4:
        return end("setup_error", "pytest_usage_error", "pytest rejected the invocation, often an unknown path or node ID.")
    if code == 3:
        return end("setup_error", "pytest_internal_error", "pytest reported an internal error.")
    if code == 2:
        kind = "collection_error" if outcome["reports"]["collect_errors"] else "interrupted"
        return end("setup_error", kind, "Test collection failed; this may be a missing dependency or a defect in the tests."
                   if kind == "collection_error" else "The pytest session was interrupted.")
    if code == 1 or counts["failed"] or counts["errors"]:
        return end("failed", "test_failures", "One or more tests failed or errored.")
    if code != 0:
        return end("failed", "unexpected_exit", f"pytest exited with status {code}.")
    if finished != 0:
        return end("setup_error", "missing_report", "pytest exited cleanly but produced no session report.")
    if counts["collected"] == 0:
        return end("no_tests_collected", "no_tests_collected", "The selectors matched no tests.")
    if counts["reported"] == 0:
        return end("setup_error", "missing_report", "Tests were collected but no per-test results were reported.")
    if counts["executed"] == 0:
        return end("no_tests_executed", "no_tests_executed", "Tests were collected but none executed (all skipped).")
    if counts["incomplete"] or counts["not_run"] or counts["passed"] < 1:
        return end("failed", "incomplete_run", "Some collected tests did not complete.")
    for item in coverage:
        if item["collected"] == 0:
            return end("no_tests_collected", "selector_matched_nothing", f"Selector matched no tests: {item['selector']}")
        if item["executed"] == 0:
            return end("no_tests_executed", "selector_not_executed", f"Selector executed no tests: {item['selector']}")
    if {c["selector"] for c in coverage} != set(selectors):
        return end("setup_error", "missing_report", "Selector coverage evidence is incomplete.")
    if overlay_paths:
        seen = {c["path"]: c["executed"] for c in outcome["overlay_coverage"]}
        missing = [p for p in overlay_paths if not seen.get(p)]
        if missing:
            return end("no_tests_executed", "overlay_not_executed",
                       "The reviewer overlay's test files did not execute: " + ", ".join(missing[:5]))
    return end("passed")


class JobService:
    """Bounded queue with one worker thread, a durable result store and lazy retention cleanup."""

    def __init__(self, cfg: RunSettings, runner: Runner | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.runner = runner or make_subprocess_runner(cfg.runner_command)
        self.clock = clock
        self.root = Path(cfg.data_dir)
        self.store = RunStore(self.root / "results")
        self.spool_root = self.root / "spool"
        self.instance = os.urandom(8).hex()  # several local agent processes may share one data directory
        self._instance_fd: int | None = None
        self._cv = threading.Condition()
        self._queue: collections.deque[QueuedJob] = collections.deque()
        self._reserved = 0
        self._cancels: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._stop = False
        self._last_purge = 0.0
        self._started = False

    # --- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        with self._cv:
            if self._started:
                return
            self.store.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.root / "instances").mkdir(exist_ok=True, mode=0o700)
            self._instance_fd = os.open(self._lock_path(self.instance), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._instance_fd, fcntl.LOCK_EX)  # held for the process lifetime: proves this instance is alive
            self._reconcile()
            self._started = True
            self._thread = threading.Thread(target=self._work, name="git-plus-test-worker", daemon=True)
            self._thread.start()

    def _lock_path(self, instance: str) -> Path:
        return self.root / "instances" / f"{instance}.lock"

    def _live_instances(self) -> set[str]:
        """Other service processes still alive on this data directory (their liveness lock cannot be taken)."""
        live: set[str] = set()
        for lock in (self.root / "instances").glob("*.lock"):
            if lock.stem == self.instance:
                continue
            try:
                fd = os.open(lock, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.add(lock.stem)
            else:
                lock.unlink(missing_ok=True)  # its owner is gone
            finally:
                os.close(fd)
        return live

    def _reconcile(self) -> None:
        """Close runs and spool that belonged to dead service instances; never touch a live sibling's."""
        live = self._live_instances() | {self.instance}
        for record in self.store.records():
            if record.get("status") in {"queued", "running"} and record.get("instance") not in live:
                self._finish(record, {"status": "setup_error", "failure": {
                    "kind": "service_restarted", "message": "Git Plus restarted before this run finished."}})
        for child in self.spool_root.iterdir():
            if child.name not in live:
                shutil.rmtree(child, ignore_errors=True)
        self.purge(force=True)

    def shutdown(self, wait: float = 10.0) -> None:
        with self._cv:
            self._stop = True
            for event in self._cancels.values():
                event.set()
            queued = list(self._queue)
            self._queue.clear()
            self._cv.notify_all()
        for job in queued:
            record = self.store.load(job.run_id)
            if record and record["status"] == "queued":
                self._finish(record, {"status": "canceled", "failure": {"kind": "shutdown", "message": "Service shut down."}})
            shutil.rmtree(job.spool, ignore_errors=True)
        if self._thread:
            self._thread.join(wait)
        if self._instance_fd is not None:
            self._lock_path(self.instance).unlink(missing_ok=True)
            shutil.rmtree(self.spool_root / self.instance, ignore_errors=True)
            os.close(self._instance_fd)
            self._instance_fd = None

    # --- admission -------------------------------------------------------------------------
    def reserve(self) -> None:
        self.start()
        self.purge()
        with self._cv:
            if self._stop:
                raise QueueFullError("The test service is shutting down.")
            if len(self._queue) + self._reserved >= self.cfg.max_queued:
                raise QueueFullError("The test queue is full; retry later.", {"max_queued": self.cfg.max_queued})
            free = shutil.disk_usage(self.root).free
            if free < self.cfg.min_free_bytes + self.cfg.max_source_bytes:
                raise QueueFullError("Insufficient disk space to stage a test run.", {"free_bytes": free})
            self._reserved += 1

    def release(self) -> None:
        with self._cv:
            self._reserved = max(self._reserved - 1, 0)

    def enqueue(self, record: dict[str, Any], job: QueuedJob) -> None:
        """Convert a held reservation into a queued job."""
        with self._cv:
            record["instance"] = self.instance
            self.store.save(record)
            self._queue.append(job)
            self._reserved = max(self._reserved - 1, 0)
            self._cv.notify()

    def spool_dir(self, run_id: str) -> Path:
        path = self.spool_root / self.instance / run_id
        path.mkdir(parents=True, mode=0o700)
        return path

    # --- retrieval -------------------------------------------------------------------------
    def get(self, run_id: str) -> dict[str, Any] | None:
        self.start()
        self.purge()
        return self.store.load(run_id)

    def cancel(self, run_id: str) -> str:
        """Cancel a queued or running run; returns the resulting state or 'not_cancelable'."""
        with self._cv:
            for job in list(self._queue):
                if job.run_id == run_id:
                    self._queue.remove(job)
                    shutil.rmtree(job.spool, ignore_errors=True)
                    record = self.store.load(run_id)
                    if record:
                        self._finish(record, {"status": "canceled", "failure": {"kind": "canceled", "message": "Canceled before execution."}})
                    return "canceled"
            event = self._cancels.get(run_id)
            if event:
                event.set()
                return "canceling"
        return "not_cancelable"

    def mutate(self, run_id: str, change: Callable[[dict[str, Any]], None]) -> None:
        with self._cv:
            record = self.store.load(run_id)
            if record:
                change(record)
                self.store.save(record)

    # --- execution -------------------------------------------------------------------------
    def _finish(self, record: dict[str, Any], outcome: dict[str, Any]) -> None:
        now = self.clock()
        record["status"] = outcome["status"]
        record["result"] = outcome
        record["timestamps"]["finished_at"] = now
        started = record["timestamps"].get("started_at")
        if started:
            record["timestamps"]["duration_seconds"] = round(now - started, 3)
        self.store.save(record)

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait(timeout=1.0)
                if self._stop:
                    return
                job = self._queue.popleft()
                cancel = self._cancels[job.run_id] = threading.Event()
            try:
                self._run_one(job, cancel)
            except BaseException as exc:  # a crashing run must never kill the worker
                record = self.store.load(job.run_id)
                if record and record["status"] not in TERMINAL:
                    self._finish(record, {"status": "setup_error", "failure": {
                        "kind": "worker_crash", "message": f"Internal worker failure: {type(exc).__name__}"}})
            finally:
                shutil.rmtree(job.spool, ignore_errors=True)
                with self._cv:
                    self._cancels.pop(job.run_id, None)

    def _run_one(self, job: QueuedJob, cancel: threading.Event) -> None:
        record = self.store.load(job.run_id)
        if not record or record["status"] != "queued":
            return
        record["status"] = "running"
        record["timestamps"]["started_at"] = self.clock()
        self.store.save(record)
        payload = self.runner(job, cancel)
        if payload.get("canceled") or cancel.is_set():
            outcome = {"status": "canceled", "failure": {"kind": "canceled", "message": "Canceled while running."}}
        else:
            outcome = classify(payload, tuple(record["selectors"]), record["requirements_sha256"],
                               tuple((record.get("overlay") or {}).get("test_paths", [])))
        self._finish(self.store.load(job.run_id) or record, outcome)

    # --- retention -------------------------------------------------------------------------
    def purge(self, *, force: bool = False) -> int:
        """Delete terminal results past their expiry; cheap enough to run lazily every minute."""
        now = self.clock()
        if not force and now - self._last_purge < 60:
            return 0
        self._last_purge = now
        removed = 0
        for record in self.store.records():
            if record.get("status") in TERMINAL and record.get("expires_at", 0) <= now:
                self.store.delete(record["run_id"])
                removed += 1
        for stale in self.store.directory.glob("*.tmp-*"):
            if now - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)
        return removed
