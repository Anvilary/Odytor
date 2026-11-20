"""Browser sessions and submission: bounded, replayable exploration of an application inside the sandbox.

The runner is stateless: every browser run starts a fresh container, serves the exact workspace tree and drives a
fresh Chromium context. A *session* therefore lives here as server-side history: the steps already accepted, the
fixed mocks and viewport, and the exact workspace version and tree it was opened on. A new run replays that
history (without re-capturing its screenshots) and then performs the new steps; the new steps join the history
only when the run finished cleanly. This keeps results deterministic and the sandbox free of long-lived state:
there is no browser process or container to orphan between calls.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from odytor import browser_spec
from odytor.config import RunSettings
from odytor.errors import SecurityError, ValidationError
from odytor.github import validate_repo
from odytor.jobs import TERMINAL, JobService, QueuedJob, new_run_id
from odytor.profile import BROWSER_PROFILE, PROFILES, RepoPolicy

import re

SESSION_ID = re.compile(r"bs-[0-9a-f]{32}")
SESSION_SCHEMA = 1
OK_STATUSES = frozenset({"passed", "completed"})
ARTIFACT_OUTPUT_BYTES = 48 * 1024 * 1024  # base64 artifacts ride in the runner's report


def new_session_id() -> str:
    return "bs-" + os.urandom(16).hex()


class SessionStore:
    """One atomic JSON file per session, owner-bound, with a bounded lifetime and a per-owner cap."""

    def __init__(self, directory: str | Path, *, ttl_hours: float = 4.0, max_per_owner: int = 5,
                 clock: Callable[[], float] = time.time) -> None:
        self.root, self.ttl, self.max_per_owner, self.clock = Path(directory), ttl_hours * 3600, max_per_owner, clock
        self._lock = threading.RLock()

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValidationError("Unknown session_id.")
        return self.root / f"{session_id}.json"

    def _save(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self._path(record["session_id"])
        tmp = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, separators=(",", ":"))
        os.replace(tmp, target)

    def _records(self) -> list[dict[str, Any]]:
        found = []
        for path in self.root.glob("bs-*.json"):
            try:
                found.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return found

    def purge(self) -> None:
        now = self.clock()
        for record in self._records():
            if record["expires_at"] <= now:
                self._path(record["session_id"]).unlink(missing_ok=True)

    def create(self, *, owner: str, repo: str, workspace_id: str, version: int, tree: str, base: dict[str, Any],
               service: JobService | None = None) -> dict[str, Any]:
        with self._lock:
            self.purge()
            raw_live = sorted((r for r in self._records() if r["owner"] == owner), key=lambda r: r["created_at"])
            live = []
            for r in raw_live:
                if service is not None and r.get("pending"):
                    r = self._reconcile(r, service)
                live.append(r)
            evicted = []
            # A session is only replay history, so at the cap the oldest one yields rather than the caller being locked out
            # for hours by a handful of one-shot runs. Sessions with a run in flight are never evicted.
            while len(live) >= self.max_per_owner:
                victim = next((r for r in live if not r["pending"]), None)
                if victim is None and service is not None:
                    # Look for any session whose pending runs are terminal in the job store
                    for r in live:
                        pending_runs = [item["run_id"] for item in r.get("pending", [])]
                        if all(service.store.load(rid) is None or (service.store.load(rid) or {}).get("status") in TERMINAL for rid in pending_runs):
                            victim = r
                            break
                if victim is None:
                    raise ValidationError("Browser session limit reached and every session has a run in flight; wait for one to finish.",
                                          {"max_per_owner": self.max_per_owner})
                self._path(victim["session_id"]).unlink(missing_ok=True)
                live.remove(victim)
                evicted.append(victim["session_id"])
            now = self.clock()
            record = {"schema": SESSION_SCHEMA, "session_id": new_session_id(), "owner": owner, "repo": repo, "workspace_id": workspace_id,
                      "workspace_version": version, "tree": tree, "base": base, "steps": [], "pending": [], "runs": [],
                      "created_at": now, "expires_at": now + self.ttl, "evicted_sessions": evicted}
            self._save(record)
            return record

    def get(self, session_id: str, owner: str, *, service: JobService | None = None) -> dict[str, Any]:
        """The caller's live session with finished runs reconciled into its history."""
        with self._lock:
            self.purge()
            try:
                record = json.loads(self._path(session_id).read_text())
            except (OSError, ValueError):
                record = None
            if record is None or record["owner"] != owner or record["expires_at"] <= self.clock():
                raise ValidationError("Unknown session_id.")
            if service is not None and record["pending"]:
                record = self._reconcile(record, service)
            return record

    def _reconcile(self, record: dict[str, Any], service: JobService) -> dict[str, Any]:
        still, changed = [], False
        for item in record["pending"]:
            run = service.store.load(item["run_id"])
            status = run["status"] if run else "lost"  # an expired or missing run can never be committed
            if status in OK_STATUSES:
                record["steps"] = (record["steps"] + item["steps"])[:browser_spec.MAX_SESSION_STEPS]
                changed = True
            elif status in TERMINAL or status == "lost":
                changed = True  # a failed, canceled or timed-out run leaves the history untouched
            else:
                still.append(item)
        if changed:
            record["pending"] = still
            self._save(record)
        return record

    def add_pending(self, record: dict[str, Any], run_id: str, steps: list[dict[str, Any]]) -> None:
        with self._lock:
            record["pending"] = record["pending"] + [{"run_id": run_id, "steps": steps}]
            record["runs"] = (record["runs"] + [run_id])[-50:]
            self._save(record)

    def close(self, session_id: str, owner: str) -> dict[str, Any]:
        record = self.get(session_id, owner)
        self._path(session_id).unlink(missing_ok=True)
        return {"session_id": session_id, "closed": True, "steps_in_history": len(record["steps"]), "runs": record["runs"]}

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        import datetime as dt
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        return {"session_id": record["session_id"], "repo": record["repo"], "workspace_id": record["workspace_id"],
                "workspace_version": record["workspace_version"], "tested_tree": record["tree"],
                "steps_in_history": len(record["steps"]), "run_in_progress": bool(record["pending"]),
                "viewport": record["base"]["viewport"], "locale": record["base"]["locale"], "timezone": record["base"]["timezone"],
                "mocks": len(record["base"]["mocks"]), "created_at": iso(record["created_at"]), "expires_at": iso(record["expires_at"])}


def submit_browser_run(repo: str, workspace_id: str, expected_version: int, steps: Any, *, workspaces: Any, sessions: SessionStore,
                       service: JobService, policies: dict[str, RepoPolicy], owner: str, cfg: RunSettings,
                       session_id: str | None = None, mocks: Any = None, viewport: Any = None, locale: str | None = None,
                       timezone: str | None = None, color_scheme: str | None = None, trace: bool | None = None,
                       video: bool | None = None, app_profile: str | None = None) -> dict[str, Any]:
    """Queue a browser run on the exact current tree of a workspace, continuing a session when one is named."""
    from odytor.run_tests import public_view  # local import: run_tests owns the shared result view

    validate_repo(repo)
    policy = policies.get(repo.strip().casefold())
    if policy is None:
        raise SecurityError("This repository has no maintainer test policy and cannot be tested.")
    settings = policy.extra.get(BROWSER_PROFILE)
    if settings is None:
        raise SecurityError("Browser runs are not enabled for this repository by the maintainer test policy.")
    serve = settings.get("serve")
    if app_profile is not None:
        profiles_map = settings.get("profiles", {})
        if app_profile not in profiles_map:
            available = ", ".join(sorted(profiles_map.keys())) or "none"
            raise ValidationError(f"Unknown application launch profile '{app_profile}'. Approved profiles: {available}.")
        serve = profiles_map[app_profile]
    if isinstance(serve, dict):
        serve = dict(serve)
        if app_profile is not None:
            serve["profile"] = app_profile
        elif "profile" not in serve:
            serve["profile"] = "default"
    if not isinstance(steps, list):
        raise ValidationError("steps must be a list.")
    if len(steps) > browser_spec.MAX_STEPS:
        raise ValidationError(f"At most {browser_spec.MAX_STEPS} new steps per run.")
    current = workspaces.get(workspace_id, owner, operation="execute")
    if current["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown workspace_id.")
    if session_id is not None:
        if any(v is not None for v in (mocks, viewport, locale, timezone, color_scheme, trace, video)):
            raise ValidationError("A session keeps the mocks, viewport, locale, timezone, trace and video setting it was opened with; "
                                  "close it and open a new one to change them.")
        session = sessions.get(session_id, owner, service=service)
        if session["workspace_id"] != workspace_id:
            raise ValidationError("Unknown session_id.")
        if session["workspace_version"] != expected_version:
            raise ValidationError(f"This session was opened on workspace version {session['workspace_version']} but expected_version is "
                                  f"{expected_version}. A session tests one exact tree; open a new session for the new version.",
                                  {"session_version": session["workspace_version"], "workspace_version": current["version"]})
        if session["pending"]:
            raise ValidationError("A run is still in progress for this session; wait for it to finish.",
                                  {"run_id": session["pending"][-1]["run_id"]})
        base = session["base"]
    else:
        probe = browser_spec.validate_scenario({"steps": [{"op": "wait_ms", "ms": 0}], "mocks": mocks or [], "viewport": viewport or {"width": 1280, "height": 720},
                                                "locale": locale or "en-US", "timezone": timezone or "UTC",
                                                "color_scheme": color_scheme or "light", "trace": bool(trace),
                                                "video": bool(video)})
        base = {k: probe[k] for k in ("mocks", "viewport", "locale", "timezone", "color_scheme", "trace", "video")}
        session = None
    history = list(session["steps"]) if session else []
    try:
        combined = browser_spec.validate_scenario({"steps": history + steps, **base}, max_steps=browser_spec.MAX_SESSION_STEPS)
    except browser_spec.SpecError as exc:
        raise ValidationError(str(exc), {"stage": "scenario"}) from exc
    new_steps = combined["steps"][len(history):]
    if not new_steps:
        raise ValidationError("steps must contain at least one new step.")
    service.reserve()
    run_id = new_run_id()
    reserved = True
    spool: Path | None = None
    try:
        spool = service.spool_dir(run_id)
        taken = workspaces.snapshot(workspace_id, owner, expected_version=expected_version, destination=spool / "source.tar",
                                    max_bytes=cfg.max_source_bytes, attach_run=run_id)
        ws = taken["record"]
        if session is None:
            session = sessions.create(owner=owner, repo=repo, workspace_id=workspace_id, version=ws["version"], tree=ws["tree"], base=base, service=service)
        elif session["workspace_version"] != ws["version"] or session["tree"] != ws["tree"]:
            raise ValidationError("The workspace changed since this session was opened.")
        scenario = {**combined, "replay": len(history)}
        now = service.clock()
        record: dict[str, Any] = {
            "schema": 1, "kind": "browser", "run_id": run_id, "owner": owner, "repo": repo, "pr": ws["pr"], "mode": "browser",
            "profile": BROWSER_PROFILE, "profile_version": PROFILES[BROWSER_PROFILE], "status": "queued",
            "requested": {"head": None, "base": None},
            "revisions": {"workspace_id": workspace_id, "workspace_version": ws["version"], "source": ws["source"]},
            "trees": {"source_tree": ws["source_tree"], "effective_tree": ws["tree"]},
            "overlay": {"supplied": False, "applied": False, "sha256": None, "paths": [], "patch_status": "none", "committed": False, "pushed": False},
            "selectors": [], "test_dirs": [], "timeout_seconds": cfg.default_timeout,
            "requirements_sha256": policy.requirements_sha256 if serve.get("kind") == "command" and policy.requirements else "",
            "browser": {"session_id": session["session_id"], "steps_total": len(combined["steps"]), "replay": len(history),
                        "new_steps": len(new_steps)},
            "artifact": taken["artifact"], "timestamps": {"created_at": now, "started_at": None, "finished_at": None},
            "expires_at": now + cfg.retention_hours * 3600, "result": None}
        header = {"schema": 1, "job_id": run_id, "repo": repo, "profile": BROWSER_PROFILE, "profile_version": PROFILES[BROWSER_PROFILE],
                  "timeout_seconds": cfg.default_timeout, "serve": serve, "scenario": scenario}
        if serve.get("kind") == "command" and policy.requirements:
            header["requirements"] = policy.requirements
        service.enqueue(record, QueuedJob(run_id, spool, header, cfg.default_timeout, max_output=ARTIFACT_OUTPUT_BYTES))
        reserved = False
        sessions.add_pending(session, run_id, new_steps)
        return {**public_view(record), "session": sessions.public(session)}
    finally:
        if reserved:
            service.release()
            if spool is not None:
                shutil.rmtree(spool, ignore_errors=True)


def wait_for_run(service: JobService, run_id: str, seconds: float, *, poll: float = 0.4) -> dict[str, Any] | None:
    """Block (bounded) until the run is terminal; returns its record, or None if it is still running."""
    deadline = time.monotonic() + max(seconds, 0)
    while True:
        record = service.store.load(run_id)
        if record is not None and record["status"] in TERMINAL:
            return record
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
