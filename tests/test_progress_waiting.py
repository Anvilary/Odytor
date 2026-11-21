"""Tests for reliable progress reporting, event streaming, and bounded waiting."""
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from odytor.config import RunSettings
from odytor.jobs import JobService, QueuedJob, RunTracker, new_run_id
from odytor.run_tests import fetch_test_result, public_view


def test_run_tracker_event_recording_and_phases():
    tracker = RunTracker("test-123", max_events=10)
    assert tracker.phase == "queued"
    assert tracker.event_seq == 0

    tracker.set_phase("running")
    assert tracker.phase == "running"
    assert tracker.event_seq == 1

    tracker.record_event({"type": "phase", "phase": "provisioning", "message": "Provisioning container"})
    assert tracker.phase == "provisioning"
    assert tracker.event_seq == 2


def test_run_tracker_counts_and_active_tests():
    tracker = RunTracker("test-123")
    tracker.record_event({"type": "collection", "total": 5})
    assert tracker.counts["collected"] == 5

    tracker.record_event({"type": "test_start", "id": "test_foo", "title": "test_foo"})
    assert len(tracker.active_tests) == 1
    assert "test_foo" in tracker.active_tests

    tracker.record_event({"type": "test_end", "id": "test_foo", "outcome": "passed", "duration_ms": 12})
    assert len(tracker.active_tests) == 0
    assert tracker.counts["executed"] == 1
    assert tracker.counts["passed"] == 1

    tracker.record_event({"type": "test_start", "id": "test_bar", "title": "test_bar"})
    tracker.record_event({"type": "test_end", "id": "test_bar", "outcome": "failed", "duration_ms": 15})
    assert tracker.counts["executed"] == 2
    assert tracker.counts["failed"] == 1


def test_run_tracker_event_log_capacity_and_pagination():
    tracker = RunTracker("test-123", max_events=5)
    for i in range(10):
        tracker.record_event({"type": "test_step", "step": i})

    assert tracker.event_seq == 10
    assert len(tracker.events) == 5
    # Events 6 to 10 are retained
    event_ids = [e["event_id"] for e in tracker.events]
    assert event_ids == [6, 7, 8, 9, 10]

    # Paginate with after_event_id
    prog = tracker.get_progress(after_event_id=7, max_events=2)
    assert [e["event_id"] for e in prog["events"]] == [8, 9]
    assert prog["events_remaining"] == 1
    assert prog["latest_event_id"] == 10


def test_run_tracker_heartbeat_updates():
    tracker = RunTracker("test-123")
    old_hb = tracker.last_heartbeat
    time.sleep(0.01)
    tracker.record_event({"type": "heartbeat", "elapsed_seconds": 1.0})
    assert tracker.last_heartbeat > old_hb


def test_run_tracker_terminal_transition():
    tracker = RunTracker("test-123")
    assert not tracker.terminal

    tracker.mark_terminal({"status": "passed", "counts": {"passed": 5}})
    assert tracker.terminal
    assert tracker.phase == "passed"
    assert tracker.outcome["status"] == "passed"


def test_run_tracker_waiter_woken_by_event():
    tracker = RunTracker("test-123")
    woken = threading.Event()

    def waiter():
        with tracker.cv:
            deadline = time.monotonic() + 2.0
            while not any(e["event_id"] > 0 for e in tracker.events) and time.monotonic() < deadline:
                tracker.cv.wait(timeout=1.0)
            if any(e["event_id"] > 0 for e in tracker.events):
                woken.set()

    t = threading.Thread(target=waiter)
    t.start()

    time.sleep(0.05)
    tracker.record_event({"type": "phase", "phase": "executing"})
    t.join(timeout=1.0)
    assert woken.is_set()


def test_run_tracker_waiter_woken_by_terminal():
    tracker = RunTracker("test-123")
    woken = threading.Event()

    def waiter():
        with tracker.cv:
            deadline = time.monotonic() + 2.0
            while not tracker.terminal and time.monotonic() < deadline:
                tracker.cv.wait(timeout=1.0)
            if tracker.terminal:
                woken.set()

    t = threading.Thread(target=waiter)
    t.start()

    time.sleep(0.05)
    tracker.mark_terminal({"status": "failed"})
    t.join(timeout=1.0)
    assert woken.is_set()


def test_job_service_get_progress_or_wait_immediate(tmp_path):
    cfg = RunSettings(runner_command=("true",), policies_path="x", data_dir=str(tmp_path / "data"))
    service = JobService(cfg)
    service.start()
    run_id = new_run_id()
    record = {
        "run_id": run_id,
        "owner": "alice",
        "repo": "owner/repo",
        "status": "queued",
        "timestamps": {"created_at": time.time(), "started_at": None, "finished_at": None},
        "mode": "head",
        "profile": "python-pytest",
        "profile_version": 1,
        "requested": {"head": "a" * 40, "base": None},
        "revisions": {},
        "trees": {},
        "overlay": {},
        "selectors": ["tests"],
    }
    job = QueuedJob(run_id, tmp_path / "spool", {}, 60)
    service.enqueue(record, job)

    # Calling with wait_seconds=0 returns immediately with live progress
    rec, prog = service.get_progress_or_wait(run_id, wait_seconds=0.0)
    assert rec["run_id"] == run_id
    assert prog is not None
    assert prog["phase"] == "queued"
    service.shutdown(wait=1.0)


def test_job_service_get_progress_or_wait_timeout(tmp_path):
    def slow_runner(job, cancel):
        cancel.wait(0.5)
        return {"report": {"harness": 1, "kind": "test"}}

    cfg = RunSettings(runner_command=("true",), policies_path="x", data_dir=str(tmp_path / "data"))
    service = JobService(cfg, runner=slow_runner)
    service.start()
    run_id = new_run_id()
    record = {
        "run_id": run_id,
        "owner": "alice",
        "repo": "owner/repo",
        "status": "queued",
        "timestamps": {"created_at": time.time(), "started_at": None, "finished_at": None},
        "mode": "head",
        "profile": "python-pytest",
        "profile_version": 1,
        "requested": {"head": "a" * 40, "base": None},
        "revisions": {},
        "trees": {},
        "overlay": {},
        "selectors": ["tests"],
    }
    job = QueuedJob(run_id, tmp_path / "spool", {}, 60)
    service.enqueue(record, job)

    started = time.monotonic()
    rec, prog = service.get_progress_or_wait(run_id, wait_seconds=0.15, after_event_id=10)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.12
    assert rec["run_id"] == run_id
    assert prog is not None
    service.shutdown(wait=1.0)


def test_job_service_drain(tmp_path):
    cfg = RunSettings(runner_command=("true",), policies_path="x", data_dir=str(tmp_path / "data"))
    service = JobService(cfg)
    service.start()

    assert not service._draining
    assert service.drain(timeout=1.0)
    assert service._draining

    # After draining, reserve() raises QueueFullError
    with pytest.raises(Exception) as exc:
        service.reserve()
    assert "draining" in str(exc.value)
    service.shutdown(wait=1.0)


def test_public_view_with_live_progress():
    record = {
        "run_id": "test-123",
        "owner": "alice",
        "repo": "owner/repo",
        "pr": 42,
        "status": "running",
        "timestamps": {"created_at": time.time(), "started_at": time.time(), "finished_at": None},
        "mode": "head",
        "profile": "python-pytest",
        "profile_version": 1,
        "requested": {"head": "a" * 40, "base": None},
        "revisions": {},
        "trees": {},
        "overlay": {},
        "selectors": ["tests"],
    }
    progress = {
        "run_id": "test-123",
        "phase": "executing_tests",
        "active_tests": [{"title": "test_one"}],
        "counts": {"collected": 10, "executed": 2, "passed": 2, "failed": 0},
        "last_heartbeat_ago_seconds": 1.2,
        "terminal": False,
        "events": [{"event_id": 1, "type": "collection", "total": 10}],
        "events_remaining": 0,
        "latest_event_id": 1,
    }
    view = public_view(record, progress=progress)
    assert view["status"] == "running"
    assert view["phase"] == "executing_tests"
    assert len(view["active_tests"]) == 1
    assert view["live_counts"]["passed"] == 2
    assert len(view["events"]) == 1


def test_public_view_max_log_bytes():
    record = {
        "run_id": "test-123",
        "owner": "alice",
        "repo": "owner/repo",
        "pr": 42,
        "status": "passed",
        "timestamps": {"created_at": time.time(), "started_at": time.time(), "finished_at": time.time()},
        "mode": "head",
        "profile": "python-pytest",
        "profile_version": 1,
        "requested": {"head": "a" * 40, "base": None},
        "revisions": {},
        "trees": {},
        "overlay": {},
        "selectors": ["tests"],
        "result": {
            "status": "passed",
            "counts": {"passed": 1},
            "log": {"head": "HEAD", "tail": "A" * 2000, "total_bytes": 2004, "truncated": False},
        },
    }
    view = public_view(record, max_log_bytes=500)
    assert len(view["log"]["tail"]) == 500
    assert view["log"]["truncated"] is True
