"""Tests for reliable progress reporting, event streaming, and bounded waiting."""
import asyncio
import contextlib
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from odytor.config import RunSettings, Settings, WorkspaceSettings
from odytor.errors import SecurityError, ValidationError
from odytor.jobs import ALLOWED_EVENT_TYPES, JobService, QueuedJob, RunTracker, new_run_id
from odytor.run_tests import fetch_test_result, public_view
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401


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
        tracker.record_event({"type": "diagnostic", "message": f"diag {i}"})

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
    assert prog["next_cursor"] == 9
    assert prog["has_more"] is True
    assert prog["cursor_gap"] is False
    assert prog["cursor_expired"] is False

    # Advance using next_cursor
    prog_next = tracker.get_progress(after_event_id=prog["next_cursor"], max_events=2)
    assert [e["event_id"] for e in prog_next["events"]] == [10]
    assert prog_next["events_remaining"] == 0
    assert prog_next["next_cursor"] == 10
    assert prog_next["has_more"] is False

    # Cursor before retained buffer indicates gap/expiration
    prog_gap = tracker.get_progress(after_event_id=2, max_events=2)
    assert prog_gap["cursor_gap"] is True
    assert prog_gap["cursor_expired"] is True
    assert [e["event_id"] for e in prog_gap["events"]] == [6, 7]
    assert prog_gap["next_cursor"] == 7
    assert prog_gap["has_more"] is True


def test_pagination_concurrent_producer():
    tracker = RunTracker("test-123", max_events=100)
    stop_event = threading.Event()

    def producer():
        seq = 0
        while not stop_event.is_set() and seq < 40:
            tracker.record_event({"type": "diagnostic", "message": f"step {seq}"})
            seq += 1
            time.sleep(0.005)

    prod_thread = threading.Thread(target=producer)
    prod_thread.start()

    collected_events = []
    cursor = 0
    while len(collected_events) < 40:
        prog = tracker.get_progress(after_event_id=cursor, max_events=10)
        for ev in prog["events"]:
            collected_events.append(ev)
        if prog["next_cursor"] is not None:
            cursor = prog["next_cursor"]
        if not prod_thread.is_alive() and not prog["has_more"]:
            break
        time.sleep(0.01)

    stop_event.set()
    prod_thread.join(timeout=1.0)
    assert len(collected_events) == 40
    ids = [e["event_id"] for e in collected_events]
    assert ids == list(range(1, 41))


def test_run_tracker_heartbeat_separation():
    tracker = RunTracker("test-123")
    old_hb = tracker.last_heartbeat
    old_prog = tracker.last_progress
    old_seq = tracker.event_seq
    time.sleep(0.01)
    tracker.record_event({"type": "heartbeat", "elapsed_seconds": 1.0})
    assert tracker.last_heartbeat > old_hb
    assert tracker.last_progress == old_prog
    assert tracker.event_seq == old_seq
    assert len(tracker.events) == 0

    woken = threading.Event()

    def waiter():
        with tracker.cv:
            deadline = time.monotonic() + 0.3
            while tracker.event_seq == old_seq and time.monotonic() < deadline:
                tracker.cv.wait(timeout=0.1)
            if tracker.event_seq > old_seq:
                woken.set()

    t = threading.Thread(target=waiter)
    t.start()
    tracker.record_event({"type": "heartbeat", "elapsed_seconds": 2.0})
    t.join(timeout=0.5)
    assert not woken.is_set()


def test_run_tracker_trust_boundary_disallows_arbitrary_types():
    tracker = RunTracker("test-123")
    tracker.record_event({"type": "malicious_type", "data": "injected"})
    assert tracker.event_seq == 0
    assert len(tracker.events) == 0

    tracker.record_event("not a dict")
    assert tracker.event_seq == 0


def test_run_tracker_field_sanitization_and_bounds():
    tracker = RunTracker("test-123")
    huge_title = "Test\x00Name\r\n" + "A" * 1000
    tracker.record_event({"type": "test_start", "id": "t1" * 200, "title": huge_title})
    assert len(tracker.events) == 1
    ev = tracker.events[0]
    assert len(ev["id"]) <= 250
    assert len(ev["title"]) <= 200
    assert "\x00" not in ev["title"]


def test_run_bounded_line_buffer_limits():
    from odytor.procs import run_bounded
    cmd = ["python3", "-c", "import sys; sys.stderr.write('x' * 100000); sys.stderr.flush()"]
    res = run_bounded(cmd, env={}, max_stdout=1024, timeout=5.0)
    assert res.returncode == 0


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

    rec, prog = service.get_progress_or_wait(run_id, owner="alice", wait_seconds=0.0)
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
    rec, prog = service.get_progress_or_wait(run_id, owner="alice", wait_seconds=0.15, after_event_id=10)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.12
    assert rec["run_id"] == run_id
    assert prog is not None
    service.shutdown(wait=1.0)


def test_job_service_cross_tenant_isolation(tmp_path):
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

    # Direct service call returns (None, None) for unauthorized or missing run
    rec, prog = service.get_progress_or_wait(run_id, owner="bob", wait_seconds=0.0)
    assert rec is None and prog is None

    rec_unknown, prog_unknown = service.get_progress_or_wait("test-00000000000000000000000000000000", owner="alice", wait_seconds=0.0)
    assert rec_unknown is None and prog_unknown is None

    # fetch_test_result raises ValidationError for unauthorized or missing run
    fake_client = MagicMock()
    fake_client.settings.is_repo_allowed.return_value = True
    with pytest.raises(ValidationError):
        fetch_test_result(fake_client, "owner/repo", run_id, service=service, owner="bob")

    with pytest.raises(ValidationError):
        fetch_test_result(fake_client, "owner/repo", "test-00000000000000000000000000000000", service=service, owner="alice")

    service.shutdown(wait=1.0)


def test_job_service_drain(tmp_path):
    cfg = RunSettings(runner_command=("true",), policies_path="x", data_dir=str(tmp_path / "data"))
    service = JobService(cfg)
    service.start()

    assert not service._draining
    assert service.drain(timeout=1.0)
    assert service._draining

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
        "last_progress_ago_seconds": 0.5,
        "terminal": False,
        "events": [{"event_id": 1, "type": "collection", "total": 10}],
        "events_remaining": 0,
        "latest_event_id": 1,
        "next_cursor": 1,
        "has_more": False,
        "cursor_gap": False,
        "cursor_expired": False,
    }
    view = public_view(record, progress=progress)
    assert view["status"] == "running"
    assert view["phase"] == "executing_tests"
    assert len(view["active_tests"]) == 1
    assert view["live_counts"]["passed"] == 2
    assert len(view["events"]) == 1
    assert view["next_cursor"] == 1
    assert view["has_more"] is False
    assert view["last_progress_ago_seconds"] == 0.5


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


def test_workspace_list_and_status_annotations_and_isolation(tmp_path, monkeypatch, local_fetch):
    from odytor.server import create_server
    repo, sha = local_fetch
    head = run(repo, "rev-parse", "HEAD").decode().strip()
    client = client_for(head)

    current_principal = "alice"

    @contextlib.contextmanager
    def fake_client(cfg, remote=False, *, repo=None):
        c = client_for(head)
        c.principal = current_principal
        yield c

    monkeypatch.setattr("odytor.server.github_client", fake_client)
    monkeypatch.setattr("odytor.server.caller_principal", lambda c, r: current_principal)
    cfg = Settings(github_token="t", github_api_url="https://api.github.com", auth_token=None,
                   default_max_diff_lines=500, default_max_diff_bytes=1048576,
                   allowed_repositories=("owner/repo",),
                   workspaces=WorkspaceSettings(data_dir=str(tmp_path / "ws"), min_free_bytes=64 * 1024 * 1024))
    server = create_server(cfg)

    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    # Verify read-only annotations
    assert tools["workspace_list"].annotations.read_only_hint is True
    assert tools["workspace_list"].annotations.destructive_hint is False
    assert tools["workspace_status"].annotations.read_only_hint is True
    assert tools["workspace_status"].annotations.destructive_hint is False

    # Mutating workspace_manage preserves destructiveHint=True and readOnlyHint=False
    assert tools["workspace_manage"].annotations.read_only_hint is False
    assert tools["workspace_manage"].annotations.destructive_hint is True

    def tool(name):
        return asyncio.run(server.get_tool(name))

    # Functional test for workspace_list and workspace_status and isolation
    current_principal = "alice"
    ws_alice = tool("workspace_create").fn(repo="owner/repo", source="pr_head", number=1, expected_head=head)
    assert "workspace_id" in ws_alice
    ws_id_alice = ws_alice["workspace_id"]

    current_principal = "bob"
    ws_bob = tool("workspace_create").fn(repo="owner/repo", source="pr_head", number=1, expected_head=head)
    assert "workspace_id" in ws_bob
    ws_id_bob = ws_bob["workspace_id"]

    # When caller is alice
    current_principal = "alice"
    list_alice = tool("workspace_list").fn()
    assert [w["workspace_id"] for w in list_alice["workspaces"]] == [ws_id_alice]

    status_alice = tool("workspace_status").fn(workspace_id=ws_id_alice)
    assert status_alice["workspace_id"] == ws_id_alice

    # Alice cannot inspect Bob's workspace
    status_bob_for_alice = tool("workspace_status").fn(workspace_id=ws_id_bob)
    assert "error" in status_bob_for_alice
    assert "Unknown workspace_id" in status_bob_for_alice["error"]
