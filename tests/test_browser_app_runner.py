"""Regression and unit tests for real-app browser execution, preflight checks, and dependency provisioning."""
import json
import os
import sys
import pytest
from pathlib import Path

from odytor import browser_spec, profile, jobs
from odytor.browser_driver import AppPreflightError, Server, _clean_env
from odytor.browser import submit_browser_run
from odytor.jobs import classify_browser, QueuedJob, new_run_id
from odytor.sandbox import validate_header, RunnerConfig


def test_clean_env_sets_pythonpath_when_env_site_exists(tmp_path, monkeypatch):
    env_site = tmp_path / "site"
    env_site.mkdir()
    monkeypatch.setattr("odytor.browser_driver.harness.ENV_SITE", str(env_site))
    env = _clean_env()
    assert env["PYTHONPATH"] == str(env_site)


def test_app_preflight_detects_missing_module_and_records_log(tmp_path):
    serve = {
        "kind": "command",
        "argv": [sys.executable, "-m", "nonexistent_odysseus_module", "--port", "9999"],
        "port": 9999,
        "ready_path": "/ready",
        "ready_timeout": 2,
        "profile": "app",
    }
    server = Server(serve)
    with pytest.raises(AppPreflightError) as exc_info:
        server.start()
    err = exc_info.value
    assert err.profile == "app"
    assert err.missing_module == "nonexistent_odysseus_module"
    assert err.interpreter == sys.executable
    assert "nonexistent_odysseus_module" in server.log.decode("utf-8", "replace")


def test_app_preflight_detects_missing_executable():
    serve = {
        "kind": "command",
        "argv": ["/nonexistent/bin/fake-python", "-m", "something"],
        "port": 9999,
        "ready_path": "/ready",
        "ready_timeout": 2,
        "profile": "custom",
    }
    server = Server(serve)
    with pytest.raises(AppPreflightError) as exc_info:
        server.start()
    err = exc_info.value
    assert err.profile == "custom"
    assert "fake-python" in err.missing_module


def test_classify_browser_preserves_preflight_failure_details():
    payload = {
        "sandbox": {
            "engine_exit_code": 0,
            "timed_out": False,
            "output_overflow": False,
            "container_seconds": 1.2,
            "engine_stderr_tail": "",
            "limits": {},
        },
        "report": {
            "harness": 1,
            "kind": "browser",
            "error": {
                "stage": "startup",
                "message": "Application launch profile 'app' failed preflight: missing module 'uvicorn' for interpreter '/usr/bin/python3'.",
                "profile": "app",
                "interpreter": "/usr/bin/python3",
                "missing_module": "uvicorn",
            },
        },
    }
    outcome = classify_browser(payload, expected_steps=1, replay=0)
    assert outcome["status"] == "setup_error"
    assert outcome["failure"]["kind"] == "startup_failure"
    assert outcome["failure"]["profile"] == "app"
    assert outcome["failure"]["interpreter"] == "/usr/bin/python3"
    assert outcome["failure"]["missing_module"] == "uvicorn"


def test_browser_spec_preserves_profile_attribute():
    serve = {
        "kind": "command",
        "argv": ["python3", "-m", "uvicorn", "app:app"],
        "port": 8765,
        "ready_path": "/index.html",
        "ready_timeout": 15,
        "profile": "app",
    }
    validated = browser_spec.validate_serve(serve)
    assert validated["profile"] == "app"


def test_sandbox_validate_header_accepts_browser_requirements_and_profile():
    cfg = RunnerConfig(
        engine="podman",
        root=Path("/tmp/gp-runner"),
        image="test-image",
        cpus=1.0,
        memory_mib=512,
        pids=256,
        work_mib=256,
        tmp_mib=128,
        max_timeout=300,
        prep_timeout=120,
        frame_timeout=30,
        max_source_bytes=64 * 1024 * 1024,
        max_queued=5,
        queue_wait=60,
    )
    header = {
        "schema": 1,
        "job_id": new_run_id(),
        "repo": "odysseus-dev/odysseus",
        "profile": "browser-playwright",
        "profile_version": profile.PROFILES["browser-playwright"],
        "timeout_seconds": 60,
        "serve": {
            "kind": "command",
            "argv": ["python3", "-m", "uvicorn", "app:app"],
            "port": 8765,
            "ready_path": "/index.html",
            "ready_timeout": 10,
            "profile": "app",
        },
        "scenario": {
            "steps": [{"op": "goto", "path": "/index.html"}],
        },
        "requirements": "pytest==8.3.5\nuvicorn==0.54.0\n",
    }
    job = validate_header(cfg, header)
    assert job["kind"] == "browser"
    assert job["serve"]["profile"] == "app"
    assert "uvicorn==0.54.0" in job["requirements"]


def test_submit_browser_run_attaches_requirements_for_command_profiles(tmp_path, monkeypatch):
    pol_file = tmp_path / "policy.toml"
    pol_file.write_text(
        '[repos."odysseus-dev/odysseus"]\n'
        'profile = "python-pytest"\n'
        'test_dirs = ["tests"]\n'
        'requirements = "reqs.txt"\n'
        '\n'
        '[repos."odysseus-dev/odysseus".browser-playwright]\n'
        'kind = "static"\n'
        'root = "static"\n'
        'port = 8765\n'
        'ready_path = "/index.html"\n'
        '\n'
        '[repos."odysseus-dev/odysseus".browser-playwright.profiles.app]\n'
        'kind = "command"\n'
        'argv = ["python3", "-m", "uvicorn", "app:app", "--port", "8765"]\n'
        'port = 8765\n'
        'ready_path = "/index.html"\n'
        'ready_timeout = 10\n'
    )
    (tmp_path / "reqs.txt").write_text("pytest==8.3.5\nuvicorn==0.54.0\n")
    policies = profile.load_policies(pol_file)
    repo_pol = policies["odysseus-dev/odysseus"]

    enqueued_job = None
    class FakeService:
        def reserve(self): pass
        def release(self): pass
        def spool_dir(self, run_id):
            p = tmp_path / "spool" / run_id
            p.mkdir(parents=True, exist_ok=True)
            return p
        def clock(self): return 1000.0
        def enqueue(self, record, job):
            nonlocal enqueued_job
            enqueued_job = job

    class FakeWorkspaces:
        def get(self, ws_id, owner, operation):
            return {"repo": "odysseus-dev/odysseus", "version": 1}
        def snapshot(self, ws_id, owner, expected_version, destination, max_bytes, attach_run):
            destination.write_bytes(b"dummy tar")
            return {
                "record": {"repo": "odysseus-dev/odysseus", "version": 1, "source": "head", "pr": 6479,
                           "source_tree": "0" * 40, "tree": "0" * 40},
                "artifact": {"artifact_id": "art-1", "name": "source.tar", "kind": "source_tar", "sha256": "0" * 64, "size_bytes": 9},
            }

    class FakeSessions:
        def get(self, *a, **k): return None
        def create(self, **k): return {"session_id": "bs-0123456789abcdef0123456789abcdef", "steps": []}
        def add_pending(self, *a): pass
        def public(self, s): return s

    from odytor.config import RunSettings
    cfg = RunSettings(runner_command=("echo",), policies_path=str(pol_file), data_dir=str(tmp_path / "runs"), default_timeout=60, max_timeout=300)

    res = submit_browser_run(
        "odysseus-dev/odysseus",
        "ws-1",
        expected_version=1,
        steps=[{"op": "goto", "path": "/index.html"}],
        workspaces=FakeWorkspaces(),
        sessions=FakeSessions(),
        service=FakeService(),
        policies=policies,
        owner="user",
        cfg=cfg,
        app_profile="app",
    )
    assert enqueued_job is not None
    assert enqueued_job.header.get("requirements") == "pytest==8.3.5\nuvicorn==0.54.0\n"
    assert enqueued_job.header.get("serve", {}).get("profile") == "app"
