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

from odytor.config import RunSettings
from odytor.diagnostics import safe_text
from odytor.errors import GitPlusError, ValidationError
from odytor.procs import run_bounded

SCHEMA = 1
TERMINAL = frozenset({"passed", "failed", "no_tests_collected", "no_tests_executed", "setup_error",
                      "timeout", "canceled", "merge_conflict", "completed"})
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
    max_output: int = 4 * 1024 * 1024  # profiles that return artifacts in their report raise this


Runner = Callable[[QueuedJob, "threading.Event"], dict[str, Any]]


def _frame(header: dict[str, Any], artifact: Path):
    encoded = json.dumps(header, separators=(",", ":")).encode()
    yield len(encoded).to_bytes(4, "big") + encoded + artifact.stat().st_size.to_bytes(8, "big")
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            yield chunk


def _probe_frame():
    encoded = json.dumps({"schema": SCHEMA, "op": "probe"}, separators=(",", ":")).encode()
    yield len(encoded).to_bytes(4, "big") + encoded + (0).to_bytes(8, "big")


def runner_environment() -> dict[str, str]:
    """The runner never inherits GitHub, OAuth or service credentials from this process."""
    env = {k: os.environ[k] for k in _RUNNER_PASSTHROUGH if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("ODYTOR_RUNNER_", "GIT_PLUS_RUNNER_"))})
    return env


def make_subprocess_runner(command: tuple[str, ...], *, extra_wait: int = 1500) -> Runner:
    def run(job: QueuedJob, cancel: threading.Event) -> dict[str, Any]:
        result = run_bounded(command, env=runner_environment(), timeout=job.timeout + extra_wait,
                             max_stdout=job.max_output, cancel=cancel,
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


def _sanitize_steps(raw: Any, limit: int = 150) -> list[dict[str, Any]]:
    steps = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or len(steps) >= limit:
            continue
        entry = {"index": _int(item.get("index"), 1000), "op": _str(item.get("op"), 24), "ok": item.get("ok") if item.get("ok") in (True, False, None) else None,
                 "ms": _int(item.get("ms"), 10**7)}
        for key, bound in (("error", 300), ("artifact", 60), ("url", 200), ("title", 200), ("reason", 120)):
            if item.get(key):
                entry[key] = _str(item.get(key), bound)
        for key in ("status", "count", "requests", "bytes"):
            if type(item.get(key)) is int:
                entry[key] = _int(item.get(key), 10**9)
        for key in ("skipped", "assertion", "replayed", "stored"):
            if item.get(key) is True:
                entry[key] = True
        steps.append(entry)
    return steps


def _sanitize_browser(report: dict[str, Any]) -> dict[str, Any]:
    """Bounded, typed copy of browser evidence. The page under test chooses these strings, so each is clipped."""
    console = report.get("console") if isinstance(report.get("console"), dict) else {}
    network = report.get("network") if isinstance(report.get("network"), dict) else {}
    entries = []
    for item in (network.get("entries") or [])[:200]:
        if isinstance(item, dict):
            entry = {"method": _str(item.get("method"), 8), "path": _str(item.get("path"), 200), "type": _str(item.get("type"), 20),
                     "mocked": item.get("mocked") is True, "blocked": item.get("blocked") is True}
            if type(item.get("status")) is int:
                entry["status"] = _int(item.get("status"), 999)
            if item.get("failed"):
                entry["failed"] = _str(item.get("failed"), 80)
            entries.append(entry)
    by_path = {_str(k, 220): _int(v, 10**6) for k, v in list((network.get("by_path") or {}).items())[:100] if isinstance(k, str)}
    snapshots = []
    for snap in (report.get("snapshots") or [])[-4:]:
        if isinstance(snap, dict):
            snapshots.append({"step": snap.get("step") if snap.get("step") == "final" else _int(snap.get("step"), 1000),
                              "url": _str(snap.get("url"), 200), "title": _str(snap.get("title"), 200),
                              "selector": _str(snap.get("selector"), 100), "aria": _str(snap.get("aria"), 12000),
                              "text": _str(snap.get("text"), 4000), "aria_truncated": snap.get("aria_truncated") is True,
                              "text_truncated": snap.get("text_truncated") is True})
    browser = report.get("browser") if isinstance(report.get("browser"), dict) else {}
    messages = [{"type": _str(m.get("type"), 12), "text": _str(m.get("text"), 300), "at": _str(m.get("at"), 120) or None,
                 "line": m.get("line") if type(m.get("line")) is int else None}
                for m in (console.get("messages") or [])[:100] if isinstance(m, dict)]
    return {
        "browser": {"name": _str(browser.get("name"), 20), "version": _str(browser.get("version"), 30),
                    "viewport": {k: _int((browser.get("viewport") or {}).get(k), 4000) for k in ("width", "height")},
                    "locale": _str(browser.get("locale"), 20), "timezone": _str(browser.get("timezone"), 40),
                    "color_scheme": _str(browser.get("color_scheme"), 10)},
        "serve": {k: v for k, v in (report.get("serve") or {}).items() if k in {"kind", "port", "ready_path", "ready_seconds"}
                  and isinstance(v, (int, float, str))},
        "console": {"messages": messages, "counts": {_str(k, 12): _int(v, 10**6) for k, v in (console.get("counts") or {}).items() if isinstance(k, str)},
                    "errors": sum(1 for m in messages if m["type"] == "error")},
        "page_errors": [_str(e, 300) for e in (report.get("page_errors") or [])[:50] if isinstance(e, str)],
        "page_errors_total": _int(report.get("page_errors_total"), 10**6),
        "network": {"total": _int(network.get("total"), 10**6), "blocked": _int(network.get("blocked"), 10**6),
                    "mocked": _int(network.get("mocked"), 10**6), "failed": _int(network.get("failed"), 10**6),
                    "by_path": by_path, "entries": entries, "entries_truncated": network.get("entries_truncated") is True},
        "snapshots": snapshots,
        "events": [{_str(k, 30): _str(v, 120) if isinstance(v, str) else v for k, v in e.items()
                    if isinstance(v, (str, int, bool))} for e in (report.get("events") or [])[:20] if isinstance(e, dict)],
    }


def _base_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    return {"runner": _sanitize_runner(payload.get("runner")), "environment": _sanitize_environment(payload.get("environment")),
            "failure": None, "counts": None, "tests": [], "selector_coverage": [], "log": None, "execution": None, "reports": {},
            "storage": _sanitize_storage(payload)}


def _common_runner_failures(payload: dict[str, Any], outcome: dict[str, Any], expected_kind: str):
    """Shared handling of runner errors, sandbox timeouts and absent reports; returns (status, failure-args) or None."""
    problem = payload.get("setup_error")
    if isinstance(problem, dict):
        return "setup_error", (_str(problem.get("stage"), 40) or "runner", _str(problem.get("message"), 300),
                               {"detail": safe_text(json.dumps(problem.get("detail", {}), default=str), 1200)})
    sandbox = payload.get("sandbox") if isinstance(payload.get("sandbox"), dict) else {}
    limits = sandbox.get("limits") if isinstance(sandbox.get("limits"), dict) else {}
    admission = payload.get("admission") if isinstance(payload.get("admission"), dict) else {}
    outcome["execution"] = {"runner_queue_wait_seconds": round(_num(admission.get("waited_seconds"), 1e6), 3),
                            "container_seconds": round(_num(sandbox.get("container_seconds"), 1e6), 3),
                            "limits": {k: (v if isinstance(v, (int, float, str)) else None) for k, v in limits.items() if isinstance(k, str)}}
    if sandbox.get("timed_out"):
        return "timeout", ("timeout", "The sandbox exceeded its execution deadline.", {})
    if sandbox.get("output_overflow"):
        return "setup_error", ("output_overflow", "The sandbox produced more output than the runner accepts.", {})
    report = payload.get("report")
    if not isinstance(report, dict) or report.get("harness") != 1 or report.get("kind") != expected_kind:
        return "setup_error", ("missing_report", "The sandbox produced no valid report.",
                               {"engine_exit_code": sandbox.get("engine_exit_code"),
                                "stderr": safe_text(_str(sandbox.get("engine_stderr_tail"), 600), 600)})
    if isinstance(report.get("error"), dict):
        err = report["error"]
        return "setup_error", ("startup_failure" if err.get("stage") == "startup" else _str(err.get("stage"), 40) or "sandbox",
                               _str(err.get("message"), 300), {})
    return None


def classify_node(payload: dict[str, Any], selectors: tuple[str, ...]) -> dict[str, Any]:
    """node --test: success needs the runner's own TAP summary showing executed, passing tests and nothing failed."""
    outcome = _base_outcome(payload)

    def end(status: str, kind: str | None = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
        outcome["status"] = status
        if kind:
            outcome["failure"] = {"kind": kind, "message": message or kind, **extra}
        return outcome

    early = _common_runner_failures(payload, outcome, "node_test")
    if early:
        return end(early[0], early[1][0], early[1][1], **early[1][2])
    report = payload["report"]
    argv = report.get("argv")
    outcome["execution"]["invocation"] = [_str(a, 300) for a in argv[:60]] if isinstance(argv, list) else []
    tap = report.get("tap") if isinstance(report.get("tap"), dict) else {}
    raw = tap.get("counts") if isinstance(tap.get("counts"), dict) else {}
    c = {k: _int(raw.get(k)) for k in ("tests", "suites", "pass", "fail", "cancelled", "skipped", "todo")}
    outcome["counts"] = {"collected": c["tests"], "executed": c["pass"] + c["fail"] + c["cancelled"], "passed": c["pass"], "failed": c["fail"],
                         "errors": c["cancelled"], "skipped": c["skipped"], "todo": c["todo"]}
    outcome["tests"] = _sanitize_tests(report.get("tests"))
    outcome["tests_total"] = _int(report.get("tests_total"))
    outcome["tests_truncated"] = outcome["tests_total"] > len(outcome["tests"])
    log = report.get("log") if isinstance(report.get("log"), dict) else {}
    outcome["log"] = {"head": _log_part(log.get("head"), 4096), "tail": _log_part(log.get("tail"), 8192),
                      "total_bytes": _int(log.get("total_bytes"), 10**13), "truncated": bool(log.get("truncated"))}
    code = report.get("exit") if type(report.get("exit")) is int else None
    outcome["execution"].update(node_exit_code=code, node_seconds=round(_num(report.get("duration_seconds"), 1e6), 3))
    vacuous = [_str(v, 200) for v in (tap.get("vacuous_files") or [])[:20] if isinstance(v, str)]
    outcome["reports"] = {"tap_summary_found": tap.get("summary_found") is True, "tap_truncated": tap.get("truncated") is True,
                          "malformed_lines": _int(tap.get("malformed_lines")), "vacuous_test_files": vacuous}
    coverage = [{"selector": _str(i.get("selector"), 512), "exists": i.get("exists") is True}
                for i in (report.get("selector_exists") or [])[:20] if isinstance(i, dict)]
    outcome["selector_coverage"] = coverage
    if report.get("timed_out"):
        return end("timeout", "timeout", "node exceeded the execution deadline.")
    missing = [c_["selector"] for c_ in coverage if not c_["exists"]]
    if missing:
        return end("no_tests_collected", "selector_matched_nothing", f"Selector does not exist in the tested tree: {missing[0]}")
    if {i["selector"] for i in coverage} != set(selectors):
        return end("setup_error", "missing_report", "Selector evidence is incomplete.")
    if code is None:
        return end("setup_error", "missing_report", "No node exit status was reported.")
    if code < 0:
        return end("failed", "killed_by_signal", f"node was killed by signal {-code} (possibly a resource limit).")
    if not outcome["reports"]["tap_summary_found"]:
        return end("setup_error", "tap_summary_missing", "node produced no complete TAP summary, so no result can be trusted.")
    if c["fail"] or c["cancelled"]:
        return end("failed", "test_failures", "One or more tests failed or were cancelled.")
    if code != 0:
        return end("failed", "unexpected_exit", f"node exited with status {code}.")
    if c["tests"] == 0:
        if vacuous:
            return end("no_tests_collected", "no_tests_collected", "Test files loaded but defined no tests (a file that merely loads is not a "
                       "passing test): " + ", ".join(vacuous[:5]))
        return end("no_tests_collected", "no_tests_collected", "The selectors matched no tests.")
    if c["pass"] == 0:
        return end("no_tests_executed", "no_tests_executed", "Tests were collected but none passed (all skipped or todo).")
    if c["pass"] + c["skipped"] + c["todo"] != c["tests"]:
        return end("failed", "incomplete_run", "The reported counts do not add up to the number of tests.")
    return end("passed")


def classify_browser(payload: dict[str, Any], expected_steps: int, replay: int) -> dict[str, Any]:
    """A browser run is green only when every step ran, a page was reached, and at least one assertion held."""
    outcome = _base_outcome(payload)

    def end(status: str, kind: str | None = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
        outcome["status"] = status
        if kind:
            outcome["failure"] = {"kind": kind, "message": message or kind, **extra}
        return outcome

    early = _common_runner_failures(payload, outcome, "browser")
    if early:
        return end(early[0], early[1][0], early[1][1], **early[1][2])
    report = payload["report"]
    outcome.update(_sanitize_browser(report))
    steps = _sanitize_steps(report.get("steps"))
    outcome["steps"] = steps
    outcome["steps_total"] = _int(report.get("steps_total"))
    outcome["replayed_steps"] = replay
    ran = [s for s in steps if s["ok"] is not None]
    new_ran = [s for s in ran if s["index"] >= replay]
    asserted = [s for s in new_ran if s["op"].startswith("assert_")]
    outcome["counts"] = {"steps": len(steps), "executed": len(ran), "passed": sum(1 for s in ran if s["ok"]),
                         "failed": sum(1 for s in ran if s["ok"] is False), "skipped": sum(1 for s in steps if s.get("skipped")),
                         "assertions": len(asserted), "assertions_passed": sum(1 for s in asserted if s["ok"])}
    outcome["execution"]["browser_seconds"] = round(_num(report.get("duration_seconds"), 1e6), 3)
    if len(steps) != expected_steps or outcome["steps_total"] != expected_steps:
        return end("setup_error", "missing_report", "The step report does not match the scenario that was submitted.")
    if report.get("timed_out"):
        return end("timeout", "timeout", "The browser run reached its deadline before all steps finished.")
    failing = next((s for s in ran if s["ok"] is False), None)
    if failing is not None:
        if failing["index"] < replay:
            return end("failed", "replay_diverged", f"Replayed step {failing['index']} ({failing['op']}) failed; the session history "
                       "no longer reproduces, so this application behaves nondeterministically.", step=failing["index"])
        kind = "assertion_failed" if failing.get("assertion") else "step_failed"
        return end("failed", kind, f"Step {failing['index']} ({failing['op']}) failed: {failing.get('error', '')}"[:300], step=failing["index"])
    if not any(s["op"] == "goto" and s["ok"] for s in ran):
        return end("no_tests_executed", "no_navigation", "No page was ever loaded, so nothing was exercised.")
    if not asserted:
        return end("completed", "no_assertions", "All steps ran, but the new steps contained no assertions, so this is exploration "
                   "evidence and not a validation result.")
    return end("passed")


def classify_job(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch on the profile's kind: each profile has its own evidence rules; none falls through to pytest's."""
    from odytor.profile import REGISTRY
    spec = REGISTRY.get(record.get("profile", ""))
    kind = spec.kind if spec else "pytest"
    if kind == "node_test":
        return classify_node(payload, tuple(record["selectors"]))
    if kind == "browser":
        return classify_browser(payload, record["browser"]["steps_total"], record["browser"]["replay"])
    return classify(payload, tuple(record["selectors"]), record["requirements_sha256"],
                    tuple((record.get("overlay") or {}).get("test_paths", [])))


class JobService:
    """Bounded queue with one worker thread, a durable result store and lazy retention cleanup."""

    def __init__(self, cfg: RunSettings, runner: Runner | None = None, clock: Callable[[], float] = time.time,
                 artifacts: Any | None = None) -> None:
        self.cfg = cfg
        self.artifacts = artifacts  # an ArtifactStore, or None when artifacts are not configured
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
        self._probe_cache: tuple[float, dict[str, Any]] | None = None

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
            self._thread = threading.Thread(target=self._work, name="odytor-test-worker", daemon=True)
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
                    "kind": "service_restarted", "message": "Odytor restarted before this run finished."}})
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
            outcome = classify_job(record, payload)
            outcome["artifacts"] = self._intake_artifacts(record, payload)
        self._finish(self.store.load(job.run_id) or record, outcome)

    def queue_depth(self) -> dict[str, int]:
        with self._cv:
            return {"queued": len(self._queue), "running": len(self._cancels), "max_queued": self.cfg.max_queued}

    # --- discovery -------------------------------------------------------------------------
    def probe_runner(self, *, max_age: float = 60.0, timeout: float = 45.0) -> dict[str, Any]:
        """Ask the runner what it can actually do. Cached briefly; never raises; executes no repository code.

        Returns {"state": ...}: ok, degraded (reachable but a check failed), unreachable, or probe_unsupported
        (an older runner that predates the probe frame). Only an `ok` state means the runner is operational.
        """
        now = self.clock()
        if self._probe_cache and now - self._probe_cache[0] < max_age:
            return self._probe_cache[1]
        result = run_bounded(self.cfg.runner_command, env=runner_environment(), timeout=timeout,
                             max_stdout=MAX_RUNNER_OUTPUT, stdin_chunks=_probe_frame(), hold_stdin=True)
        if result.timed_out or result.returncode != 0 or result.overflowed:
            report: dict[str, Any] = {"state": "unreachable", "detail": safe_text(result.stderr_tail.decode("utf-8", "replace"), 200)}
        else:
            try:
                payload = json.loads(result.stdout)
            except ValueError:
                payload = None
            facts = payload.get("probe") if isinstance(payload, dict) else None
            problem = payload.get("setup_error") if isinstance(payload, dict) else None
            if isinstance(facts, dict):
                report = {"state": "ok" if facts.get("operational") is True else "degraded", "facts": facts}
            elif isinstance(problem, dict):
                report = {"state": "probe_unsupported" if problem.get("stage") == "validation" else "degraded",
                          "detail": _str(problem.get("message"), 200)}
            else:
                report = {"state": "degraded", "detail": "The runner returned an unrecognized probe response."}
        self._probe_cache = (now, report)
        return report

    def _intake_artifacts(self, record: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Store runner-reported artifacts after re-deriving everything about them; failures are reported, never raised."""
        report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
        items = report.get("artifacts") if isinstance(report.get("artifacts"), list) else []
        if not items:
            return []
        stored: list[dict[str, Any]] = []
        if self.artifacts is None:
            return [{"error": "Artifact storage is not configured; the runner's artifacts were discarded.", "discarded": len(items)}]
        ref = record.get("revisions") or {}
        context = {"run_id": record["run_id"], "workspace_id": ref.get("workspace_id"), "workspace_version": ref.get("workspace_version"),
                   "source_revision": {k: v for k, v in (ref.get("source") or {}).items() if k in {"kind", "head_sha", "head_repo", "base_sha"}},
                   "tested_tree": (record.get("trees") or {}).get("effective_tree")}
        limits, total = self.artifacts.limits, 0
        import base64, binascii
        for item in items[:limits.max_per_run]:
            if not isinstance(item, dict) or not isinstance(item.get("b64"), str):
                continue
            name = _str(item.get("name"), 80)
            try:
                if len(item["b64"]) > (limits.max_artifact_bytes * 4) // 3 + 8:
                    raise ValidationError("artifact exceeds the per-artifact limit")
                data = base64.b64decode(item["b64"], validate=True)
                total += len(data)
                if total > limits.max_run_bytes:
                    raise ValidationError("run artifact budget exceeded")
                meta = self.artifacts.put(owner=record["owner"], repo=record["repo"], kind=_str(item.get("kind"), 20), name=name,
                                          data=data, context=context)
                stored.append(self.artifacts.public(meta))
            except (ValidationError, binascii.Error, ValueError) as exc:
                stored.append({"name": name, "error": safe_text(str(exc), 160)})
        if len(items) > limits.max_per_run:
            stored.append({"error": f"{len(items) - limits.max_per_run} further artifact(s) were not stored (limit {limits.max_per_run})."})
        return stored

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
