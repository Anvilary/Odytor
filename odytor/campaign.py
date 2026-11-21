"""Bounded, disposable validation campaigns for AI agents and maintainers.

Orchestrates the complete inspection-to-evidence loop in a single turn without intermediate
human confirmations:
1. Validates repository access and pins exact revisions (head SHA, base SHA).
2. Provisions a temporary, isolated workspace with NO publication authority.
3. Optionally applies a disposable overlay patch or edits.
4. Executes approved test profiles (python-pytest, node-test, linters, typecheckers, etc.).
5. Optionally executes headless browser scenarios with network assertion tracking and screenshot capture.
6. Collects all structured evidence, logs, and artifacts into the caller's private artifact store.
7. Guarantees deterministic cleanup of the disposable workspace in all terminal states (success, failure, timeout, cancellation).
8. Strictly prevents GitHub mutations: the campaign and its runner sandboxes possess zero GitHub write credentials.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from odytor.browser import submit_browser_run, wait_for_run
from odytor.errors import GitPlusError, SecurityError, ValidationError
from odytor.github import GitHubClient
from odytor.git_workspace import pr_context
from odytor.jobs import JobService, new_run_id
from odytor.profile import PLAYWRIGHT_SPEC_PROFILE, PROFILE_NAME, PROFILES, REGISTRY, ProfileError, RepoPolicy
from odytor.publish import PlanStore, plan_publication
from odytor.run_tests import cancel_test_run, submit_workspace_run, workspace_playwright_selectors
from odytor.workspace import open_workspace

logger = logging.getLogger(__name__)


TERMINAL = frozenset({"passed", "failed", "setup_error", "timeout", "canceled", "completed"})
ACTIVE = frozenset({"running", "canceling"})
DEFAULT_RETENTION_HOURS = 48.0
PLAN_RELEASE_SECONDS = 2 * 3600   # a retained publication workspace is released this long after a campaign ends, once its plan is no longer usable
_MAINTAIN_INTERVAL = 30.0
_IDX = re.compile(r"^idx_[0-9a-f]{40}\.id$")


class CampaignStopped(Exception):
    """The persisted campaign is already terminal (canceled or reconciled elsewhere); the worker must not write over it."""


@dataclass
class CampaignRecord:
    campaign_id: str
    owner: str
    repo: str
    number: int
    mode: str
    status: str  # running | canceling | passed | failed | setup_error | timeout | canceled | completed
    created_at: float
    updated_at: float
    finished_at: float | None = None
    requested_head: str | None = None
    requested_base: str | None = None
    tested_head: str | None = None
    tested_base: str | None = None
    tested_tree: str | None = None
    workspace_id: str | None = None
    current_run_id: str | None = None
    idempotency_key: str | None = None
    request_digest: str | None = None
    instance: str | None = None
    cancel_requested: bool = False
    validation_executed: bool = False
    push_plan: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    browser: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    workspace_cleaned: bool = False
    github_mutations: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data = {k: getattr(self, k) for k in _PERSISTED}
        data["github_mutations"] = "none"
        data["green"] = self.status == "passed" and self.validation_executed
        data["cleanup"] = self.cleanup_state()
        return data

    def cleanup_state(self) -> str:
        if not self.workspace_id or self.workspace_cleaned:
            return "done" if self.workspace_cleaned else "none"
        if self.status in TERMINAL and self.push_plan is not None:
            return "workspace_retained_for_publish"
        return "pending"


_PERSISTED = (
    "campaign_id", "owner", "repo", "number", "mode", "status", "created_at", "updated_at", "finished_at", "requested_head",
    "requested_base", "tested_head", "tested_base", "tested_tree", "workspace_id", "current_run_id", "idempotency_key",
    "request_digest", "instance", "cancel_requested", "validation_executed", "push_plan", "steps", "results", "browser",
    "artifacts", "failure", "workspace_cleaned")


def request_digest(**request: Any) -> str:
    """Canonical digest of the effective request; the idempotency key is bound to it."""
    if request.get("profiles") is None:
        request["profiles"] = [PROFILE_NAME]
    profiles = request["profiles"]
    request["profile_selectors"] = {profile: selectors for profile, selectors in (request.get("profile_selectors") or {}).items()
                                    if profile in profiles and selectors}
    for field in ("expected_head", "expected_base"):
        if request.get(field):
            request[field] = request[field].lower()
    if request.get("mode") == "pr_head":
        request["expected_base"] = None
    if not request.get("propose_push"):
        request["commit_message"] = None
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CampaignStore:
    """Durable campaign records with one read-modify-write path.

    Every write happens under a store-wide lock (a thread lock plus flock, so sibling processes are covered), terminal
    records are never overwritten by a worker that still holds an older copy, and idempotency keys are claimed
    atomically against a digest of the request.
    """

    def __init__(self, root: Path, clock=time.time, retention_hours: float = DEFAULT_RETENTION_HOURS):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.retention_seconds = retention_hours * 3600
        self._tlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None
        self._last_maintain = 0.0

    @contextlib.contextmanager
    def _exclusive(self):
        with self._tlock:
            if self._depth == 0:
                self._fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    os.close(self._fd)
                    self._fd = None

    def _path(self, campaign_id: str) -> Path:
        if not isinstance(campaign_id, str) or not re.fullmatch(r"cmp-[0-9a-f]{1,64}|cmp-test[0-9a-z]*", campaign_id):
            raise ValidationError("Unknown campaign.")
        return self.root / f"{campaign_id}.json"

    @staticmethod
    def _idx_name(owner: str, repo: str, key: str) -> str:
        material = f"{owner}\0{repo.strip().casefold()}\0{key}"
        return "idx_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40] + ".id"

    def _write(self, record: CampaignRecord) -> None:
        target = self._path(record.campaign_id)
        tmp = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, indent=2)
        os.replace(tmp, target)

    def save(self, record: CampaignRecord) -> None:
        """Unconditional write (creation and tests). Workers use checkpoint()."""
        with self._exclusive():
            self._write(record)

    def load(self, campaign_id: str, owner: str) -> CampaignRecord | None:
        try:
            p = self._path(campaign_id)
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError):
            return None
        if data.get("owner") != owner:
            return None
        try:
            return CampaignRecord(**{k: v for k, v in data.items() if k in _PERSISTED})
        except TypeError:
            return None

    def records(self) -> list[CampaignRecord]:
        found = []
        for f in self.root.glob("cmp-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                found.append(CampaignRecord(**{k: v for k, v in data.items() if k in _PERSISTED}))
            except (OSError, ValueError, TypeError):
                continue
        return found

    # --- idempotent creation -----------------------------------------------------------------------------------
    def find_by_idempotency_key(self, owner: str, repo: str, idempotency_key: str) -> CampaignRecord | None:
        idx = self.root / self._idx_name(owner, repo, idempotency_key)
        try:
            return self.load(idx.read_text(encoding="utf-8").strip(), owner)
        except OSError:
            return None

    def claim(self, record: CampaignRecord) -> tuple[CampaignRecord, bool]:
        """Create the record, or return the existing one for the same key. A key reused with different content is refused."""
        with self._exclusive():
            if not record.idempotency_key:
                self._write(record)
                return record, True
            idx = self.root / self._idx_name(record.owner, record.repo, record.idempotency_key)
            while True:
                existing = self.find_by_idempotency_key(record.owner, record.repo, record.idempotency_key)
                if existing is not None:
                    if existing.request_digest != record.request_digest:
                        raise ValidationError(
                            "This idempotency_key was already used for a different request; use a new key for a different request.",
                            {"campaign_id": existing.campaign_id, "classification": "idempotency_key_conflict"})
                    return existing, False
                idx.unlink(missing_ok=True)  # missing or purged target: the claim is stale
                self._write(record)
                try:
                    fd = os.open(idx, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    self._path(record.campaign_id).unlink(missing_ok=True)
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(record.campaign_id)
                return record, True

    # --- worker writes -----------------------------------------------------------------------------------------
    def checkpoint(self, record: CampaignRecord) -> None:
        """Persist a worker's copy without ever overwriting a cancel request or a terminal state written elsewhere."""
        with self._exclusive():
            current = self.load(record.campaign_id, record.owner)
            if current is not None:
                if current.status in TERMINAL:
                    raise CampaignStopped(current.status)
                if current.cancel_requested:
                    record.cancel_requested = True
                    record.status = current.status if current.status == "canceling" else record.status
            record.updated_at = self.clock()
            self._write(record)

    def update(self, campaign_id: str, owner: str, change) -> CampaignRecord | None:
        with self._exclusive():
            record = self.load(campaign_id, owner)
            if record is None:
                return None
            before = record.status
            change(record)
            if before in TERMINAL and record.status != before:
                return self.load(campaign_id, owner)  # terminal states are final
            record.updated_at = self.clock()
            self._write(record)
            return record

    def finalize(self, record: CampaignRecord) -> CampaignRecord:
        """The worker's last write: a requested cancellation always wins over the computed outcome."""
        with self._exclusive():
            current = self.load(record.campaign_id, record.owner)
            if current is not None and current.status in TERMINAL:
                return current
            if (current is not None and current.cancel_requested) or record.cancel_requested:
                record.cancel_requested = True
                record.status = "canceled"
                record.failure = {"kind": "canceled", "message": "Campaign canceled by caller."}
            record.finished_at = self.clock()
            record.updated_at = record.finished_at
            self._write(record)
            return record

    # --- cancellation, recovery, retention ---------------------------------------------------------------------
    def cancel(self, campaign_id: str, owner: str, *, service: JobService, workspaces: Any) -> dict[str, Any]:
        with self._exclusive():
            record = self.load(campaign_id, owner)
            if record is None:
                raise ValidationError(f"Unknown campaign '{campaign_id}'.")
            if record.status in TERMINAL:
                return {"campaign_id": campaign_id, "status": record.status, "already_finished": True,
                        "workspace_cleaned": record.workspace_cleaned}
            record.cancel_requested = True
            if record.current_run_id:
                try:
                    cancel_test_run(record.current_run_id, service=service, owner=owner)
                except Exception as exc:
                    logger.warning("Failed to cancel child run %s: %s", record.current_run_id, exc)
            if self._worker_alive(record, service):
                record.status = "canceling"   # the worker finishes teardown; cleanup is reported as pending until it does
                record.failure = {"kind": "canceling", "message": "Cancellation requested; waiting for the child run and workspace teardown."}
            else:
                self._close_out(record, workspaces, "canceled", {"kind": "canceled", "message": "Campaign canceled by caller."})
            record.updated_at = self.clock()
            self._write(record)
            return {"campaign_id": campaign_id, "status": record.status, "workspace_cleaned": record.workspace_cleaned,
                    "cleanup": record.to_dict()["cleanup"], "child_run_id": record.current_run_id}

    @staticmethod
    def _worker_alive(record: CampaignRecord, service: JobService) -> bool:
        try:
            return bool(record.instance) and record.instance in service.live_instances()
        except Exception:
            return False

    def _close_out(self, record: CampaignRecord, workspaces: Any, status: str, failure: dict[str, Any]) -> None:
        keep = record.push_plan is not None and status != "canceled"
        if record.workspace_id and not record.workspace_cleaned and not keep:
            try:
                workspaces.close(record.workspace_id, record.owner)
                record.workspace_cleaned = True
            except GitPlusError:
                record.workspace_cleaned = True  # already expired or closed: nothing left to release
            except Exception as exc:
                logger.warning("Failed to close workspace %s: %s", record.workspace_id, exc)
                failure = {**failure, "cleanup_error": str(exc)[:200]}
        record.status, record.failure, record.finished_at = status, failure, self.clock()
        record.current_run_id = None

    def maintain(self, *, service: JobService, workspaces: Any, plans: Any | None = None, force: bool = False) -> dict[str, int]:
        """Reconcile campaigns whose worker is gone, release retained workspaces nothing can use, and purge old records."""
        now = self.clock()
        if not force and now - self._last_maintain < _MAINTAIN_INTERVAL:
            return {}
        self._last_maintain = now
        out = {"reconciled": 0, "released": 0, "purged": 0}
        with self._exclusive():
            try:
                live = service.live_instances()
            except Exception:
                live = None
            for record in self.records():
                if record.status in ACTIVE and live is not None and record.instance not in live:
                    if record.current_run_id:
                        try:
                            cancel_test_run(record.current_run_id, service=service, owner=record.owner)
                        except Exception:
                            pass
                    was_cancel = record.cancel_requested
                    if record.current_run_id:
                        record.steps.append({"name": "child_reconciliation", "status": "service_restarted",
                                             "run_id": record.current_run_id})
                    self._close_out(record, workspaces, "canceled" if was_cancel else "setup_error",
                                    {"kind": "canceled" if was_cancel else "service_restarted",
                                     "message": "Campaign canceled by caller." if was_cancel else
                                     "Odytor restarted before this campaign finished. The runner reclaims disconnected execution. "
                                     "Use a new idempotency_key to run it again."})
                    record.updated_at = now
                    self._write(record)
                    out["reconciled"] += 1
                elif (record.status in TERMINAL and record.push_plan is not None and record.workspace_id and not record.workspace_cleaned
                      and now - (record.finished_at or now) > PLAN_RELEASE_SECONDS and self._plan_unusable(record, plans)):
                    try:
                        workspaces.close(record.workspace_id, record.owner)
                    except GitPlusError:
                        pass
                    except Exception as exc:
                        logger.warning("Failed to release workspace %s: %s", record.workspace_id, exc)
                        continue
                    record.workspace_cleaned = True
                    self._write(record)
                    out["released"] += 1
                if record.status in TERMINAL and now - (record.finished_at or record.updated_at) > self.retention_seconds and (
                        record.workspace_cleaned or not record.workspace_id or record.push_plan is None):
                    self._path(record.campaign_id).unlink(missing_ok=True)
                    out["purged"] += 1
            for idx in self.root.glob("idx_*.id"):
                try:
                    target = idx.read_text(encoding="utf-8").strip()
                    stale = not _IDX.match(idx.name) or not (self.root / f"{target}.json").exists()
                except OSError:
                    stale = True
                if stale:
                    idx.unlink(missing_ok=True)
        return out

    @staticmethod
    def _plan_unusable(record: CampaignRecord, plans: Any | None) -> bool:
        plan_id = (record.push_plan or {}).get("plan_id")
        if plans is None or not plan_id:
            return True
        try:
            plan = plans.get(plan_id, record.owner)
        except GitPlusError:
            return True  # purged or expired
        return plan.get("state") in {"applied", "rejected"} or (plan.get("state") == "pending" and plan.get("expires_at", 0) <= plans.clock())


def preflight_campaign(client: GitHubClient, repo: str, number: int, *, expected_head: str | None, expected_base: str | None,
                       mode: str, profiles: list[str] | None, policies: dict[str, RepoPolicy], propose_push: bool,
                       commit_message: str | None, plans: Any | None, idempotency_key: str | None,
                       profile_selectors: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Everything that can be refused before any state exists. Returns the resolved profiles and live revisions."""
    requested_profiles = ["python-pytest"] if profiles is None else list(profiles)
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not (1 <= len(idempotency_key) <= 128) or not re.match(r"^[a-zA-Z0-9_.-]+$", idempotency_key):
            raise ValidationError("idempotency_key must be 1-128 alphanumeric, underscore, dot, or hyphen characters.")
    if propose_push:
        if mode != "pr_head":
            raise ValidationError("propose_push requires mode='pr_head'.")
        if not commit_message or not isinstance(commit_message, str) or not commit_message.strip():
            raise ValidationError("commit_message is required when propose_push=True.")
        if plans is None:
            raise ValidationError("Plan storage is not configured on this server.")
    for prof in requested_profiles:
        if prof not in PROFILES:
            raise ValidationError(f"Unknown execution profile '{prof}'. Available: {', '.join(PROFILES)}.")
        if REGISTRY[prof].kind == "browser":
            raise ValidationError("Browser profile must be specified via browser_scenario, not profiles list.")
    policy = policies.get(repo.strip().casefold())
    if policy is None:
        raise SecurityError(f"Repository '{repo}' has no maintainer test policy.")
    for prof in requested_profiles:
        if prof not in policy.profiles:
            raise ValidationError(f"Maintainer policy for '{repo}' does not enable profile '{prof}'. Enabled: {', '.join(policy.profiles)}.")
        if prof == PLAYWRIGHT_SPEC_PROFILE:
            try:
                workspace_playwright_selectors(policy, (profile_selectors or {}).get(prof))
            except ProfileError as exc:
                raise ValidationError(str(exc)) from exc
    ctx = pr_context(client, repo, number, expected_head=expected_head, write=False)
    live_head, live_base = ctx["head_sha"], ctx["base_sha"]
    if expected_head and expected_head.lower() != live_head.lower():
        raise ValidationError(f"PR head has moved: expected {expected_head}, found {live_head}.")
    if mode == "prospective_merge":
        if not expected_base:
            raise ValidationError("expected_base (the live target-branch tip) is required for mode='prospective_merge'.")
        base_ref = ctx.get("base_ref")
        if base_ref and hasattr(client, "request"):
            try:
                resp = client.request("GET", f"repos/{repo}/git/ref/heads/{base_ref}")
                ref_data = resp.json() if hasattr(resp, "json") else {}
                live_tip = ref_data.get("object", {}).get("sha") if isinstance(ref_data, dict) else None
                if live_tip and expected_base.lower() != live_tip.lower():
                    raise ValidationError(f"Target branch '{base_ref}' tip has moved: expected {expected_base}, found live tip {live_tip}.")
            except Exception as exc:
                if isinstance(exc, ValidationError):
                    raise
    return {"profiles": requested_profiles, "live_head": live_head, "live_base": live_base}


def _cancel_child(service: JobService, run_id: str | None, owner: str) -> None:
    if run_id:
        try:
            cancel_test_run(run_id, service=service, owner=owner)
        except Exception as exc:
            logger.warning("Failed to cancel child run %s: %s", run_id, exc)


def _await_child_stop(service: JobService, run_id: str, owner: str) -> None:
    _cancel_child(service, run_id, owner)
    # Keep the workspace and child identity until the execution worker reports that it released the slot.
    while wait_for_run(service, run_id, 30) is None:
        logger.warning("Still waiting for child run %s to release its slot after cancellation", run_id)


def run_validation_campaign(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    expected_head: str | None = None,
    expected_base: str | None = None,
    mode: Literal["pr_head", "prospective_merge"] = "pr_head",
    profiles: list[str] | None = None,
    profile_selectors: dict[str, list[str]] | None = None,
    browser_scenario: list[dict[str, Any]] | None = None,
    browser_mocks: list[dict[str, Any]] | None = None,
    browser_video: bool = False,
    app_profile: str | None = None,
    overlay_patch: str | None = None,
    timeout_seconds: int = 300,
    workspaces: Any,
    service: JobService,
    sessions: Any,
    policies: dict[str, RepoPolicy],
    owner: str,
    cfg: Any,
    campaign_store: CampaignStore | None = None,
    campaign_id: str | None = None,
    idempotency_key: str | None = None,
    propose_push: bool = False,
    commit_message: str | None = None,
    plans: PlanStore | None = None,
    digest: str | None = None,
) -> dict[str, Any]:
    """Execute the bounded disposable validation campaign."""
    now = time.time()
    cid = campaign_id or f"cmp-{secrets.token_hex(16)}"
    pre = preflight_campaign(client, repo, number, expected_head=expected_head, expected_base=expected_base, mode=mode, profiles=profiles,
                             policies=policies, propose_push=propose_push, commit_message=commit_message, plans=plans,
                             idempotency_key=idempotency_key, profile_selectors=profile_selectors)
    requested_profiles, live_head = pre["profiles"], pre["live_head"]
    instance = getattr(service, "instance", None)
    instance = instance if isinstance(instance, str) else None

    record = campaign_store.load(cid, owner) if campaign_store and campaign_id else None
    if record is not None:
        record.requested_head = live_head
        record.requested_base = expected_base if mode == "prospective_merge" else None
        record.instance = instance
    else:
        record = CampaignRecord(
            campaign_id=cid, owner=owner, repo=repo, number=number, mode=mode, status="running", created_at=now, updated_at=now,
            idempotency_key=idempotency_key, request_digest=digest, instance=instance, requested_head=live_head,
            requested_base=expected_base if mode == "prospective_merge" else None)

    def persist() -> None:
        if campaign_store:
            campaign_store.checkpoint(record)

    def canceled() -> bool:
        if campaign_store:
            current = campaign_store.load(cid, owner)
            if current is not None and current.cancel_requested:
                record.cancel_requested = True
        return record.cancel_requested

    ws_id = None
    b_session_id = None
    all_passed = True
    executed = 0
    deadline = time.monotonic() + timeout_seconds
    stopped = False

    try:
        persist()
        # Step 1: Create disposable workspace
        step_t0 = time.monotonic()
        ws_operations = ["read", "search", "edit", "diff", "execute", "artifacts", "manage"]
        if propose_push:
            ws_operations.append("publish")
        ws_kwargs: dict[str, Any] = {"source": mode, "number": number, "expected_head": live_head, "operations": ws_operations}
        if mode == "prospective_merge":
            ws_kwargs["expected_base"] = expected_base
        if canceled():
            raise CampaignStopped("canceled")
        ws = open_workspace(client, repo, service=workspaces, owner=owner, **ws_kwargs)
        ws_id = ws["workspace_id"]
        expected_version = ws["version"]
        record.workspace_id = ws_id
        record.tested_head = live_head
        record.tested_base = expected_base if mode == "prospective_merge" else None
        record.tested_tree = ws["tree"]
        record.steps.append({"name": "workspace_create", "status": "passed", "duration_seconds": round(time.monotonic() - step_t0, 3),
                             "workspace_id": ws_id, "tree": ws["tree"]})
        persist()  # from here the workspace identity is durable, so cancel and recovery can release it

        # Step 2: Apply overlay patch if supplied
        if overlay_patch:
            step_t0 = time.monotonic()
            patch_res = workspaces.apply_patch(ws_id, owner, expected_version=expected_version, patch=overlay_patch)
            expected_version = patch_res["version"]
            record.tested_tree = patch_res["tree"]
            record.steps.append({"name": "apply_overlay", "status": "passed", "duration_seconds": round(time.monotonic() - step_t0, 3),
                                 "patch_bytes": len(overlay_patch), "new_tree": patch_res["tree"]})
            persist()

        # Step 3: Run execution profiles sequentially
        for prof in requested_profiles:
            if canceled():
                raise CampaignStopped("canceled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Validation campaign exceeded total execution timeout.")
            step_t0 = time.monotonic()
            selectors = (profile_selectors or {}).get(prof)
            sub = submit_workspace_run(repo, ws_id, expected_version, workspaces=workspaces, service=service, policies=policies, owner=owner,
                                       cfg=cfg.testing, profile=prof, selectors=selectors, admitted_campaign=True)
            run_id = sub["run_id"]
            record.current_run_id = run_id
            persist()  # the child run id is durable before waiting, so it can be monitored and canceled
            if canceled():
                _await_child_stop(service, run_id, owner)
                raise CampaignStopped("canceled")
            final = wait_for_run(service, run_id, deadline - time.monotonic())
            dur = round(time.monotonic() - step_t0, 3)
            if final is None:
                _await_child_stop(service, run_id, owner)
                record.steps.append({"name": f"profile:{prof}", "status": "timeout", "duration_seconds": dur, "run_id": run_id})
                record.results[prof] = {"status": "timeout", "run_id": run_id}
                raise TimeoutError("Validation campaign exceeded total execution timeout.")
            if canceled():
                raise CampaignStopped("canceled")
            executed += 1
            status = final.get("status", "unknown")
            res = final.get("result") or {}
            record.steps.append({"name": f"profile:{prof}", "status": status, "duration_seconds": dur, "run_id": run_id,
                                 "failure": res.get("failure") or final.get("failure")})
            record.results[prof] = {"status": status, "run_id": run_id, "duration_seconds": dur,
                                    "failure": res.get("failure") or final.get("failure"),
                                    "counts": res.get("counts") or final.get("counts"),
                                    "diagnostics_summary": res.get("diagnostics_summary") or final.get("diagnostics_summary")}
            for art in (res.get("artifacts") or final.get("artifacts") or []):
                record.artifacts.append(art)
            persist()
            if status != "passed":
                all_passed = False
                break

        # Step 4: Run browser scenario if requested and all tests passed (or if only browser was requested)
        if browser_scenario and all_passed:
            if canceled():
                raise CampaignStopped("canceled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Validation campaign exceeded total execution timeout before browser run.")
            step_t0 = time.monotonic()
            sub_b = submit_browser_run(repo, ws_id, expected_version, browser_scenario, workspaces=workspaces, sessions=sessions,
                                       service=service, policies=policies, owner=owner, cfg=cfg.testing, mocks=browser_mocks,
                                       video=browser_video, app_profile=app_profile, admitted_campaign=True)
            b_run_id = sub_b["run_id"]
            b_session_id = (sub_b.get("session") or {}).get("session_id")
            record.current_run_id = b_run_id
            persist()
            if canceled():
                _await_child_stop(service, b_run_id, owner)
                raise CampaignStopped("canceled")
            final_b = wait_for_run(service, b_run_id, deadline - time.monotonic())
            dur = round(time.monotonic() - step_t0, 3)
            if final_b is None:
                _await_child_stop(service, b_run_id, owner)
                record.steps.append({"name": "browser-playwright", "status": "timeout", "duration_seconds": dur, "run_id": b_run_id})
                record.browser = {"status": "timeout", "run_id": b_run_id}
                raise TimeoutError("Validation campaign exceeded total execution timeout during its browser run.")
            else:
                if canceled():
                    raise CampaignStopped("canceled")
                executed += 1
                b_status = final_b.get("status", "unknown")
                res_b = final_b.get("result") or {}
                b_failure = res_b.get("failure") or final_b.get("failure") or res_b.get("error") or final_b.get("error")
                record.steps.append({"name": "browser-playwright", "status": b_status, "duration_seconds": dur, "run_id": b_run_id,
                                     "failure": b_failure})
                record.browser = {"status": b_status, "run_id": b_run_id, "duration_seconds": dur,
                                  "counts": res_b.get("counts") or final_b.get("counts"), "network": res_b.get("network") or final_b.get("network"),
                                  "failure": b_failure, "error": res_b.get("error") or final_b.get("error"),
                                  "serve": res_b.get("serve") or final_b.get("serve")}
                for art in (res_b.get("artifacts") or final_b.get("artifacts") or []):
                    record.artifacts.append(art)
                if b_status != "passed":
                    all_passed = False
            persist()

        # Step 5: If propose_push requested and all tests passed, plan publication
        if propose_push and all_passed and executed:
            if canceled():
                raise CampaignStopped("canceled")
            step_t0 = time.monotonic()
            plan_res = plan_publication(client, repo, ws_id, expected_version, commit_message, workspaces=workspaces, plans=plans, jobs=service,
                                        owner=owner, max_files=getattr(cfg, "quick_fix_max_files", 10),
                                        max_changed_lines=getattr(cfg, "quick_fix_max_changed_lines", 1000),
                                        max_patch_bytes=getattr(cfg, "quick_fix_max_patch_bytes", 262144))
            record.push_plan = plan_res
            record.steps.append({"name": "publish_plan", "status": "passed", "duration_seconds": round(time.monotonic() - step_t0, 3),
                                 "plan_id": plan_res.get("plan_id")})
            persist()

    except CampaignStopped:
        stopped = True
        all_passed = False
    except TimeoutError as exc:
        record.status = "timeout"
        record.failure = {"kind": "timeout", "message": str(exc)}
        all_passed = False
    except GitPlusError as exc:
        record.status = "setup_error"
        record.failure = exc.to_dict()
        all_passed = False
    except Exception as exc:
        record.status = "setup_error"
        record.failure = {"kind": "unexpected", "message": str(exc)}
        all_passed = False
    finally:
        if b_session_id and sessions:
            try:
                sessions.close(b_session_id, owner)
            except Exception:
                pass
        # Cleanup runs under every outcome; the plan keeps its workspace only when the campaign succeeded and a plan exists.
        keep = record.push_plan is not None and all_passed and not canceled()
        if ws_id:
            if keep:
                record.workspace_cleaned = False
                record.steps.append({"name": "workspace_cleanup", "status": "preserved_for_publish", "workspace_id": ws_id})
            else:
                try:
                    workspaces.close(ws_id, owner)
                    record.workspace_cleaned = True
                    record.steps.append({"name": "workspace_cleanup", "status": "passed", "closed": True})
                except GitPlusError as exc:
                    record.workspace_cleaned = "expired" in str(exc).lower() or "unknown workspace" in str(exc).lower()
                    record.steps.append({"name": "workspace_cleanup", "status": "failed", "error": str(exc)})
                except Exception as exc:
                    logger.error("Failed to dispose workspace %s: %s", ws_id, exc)
                    record.steps.append({"name": "workspace_cleanup", "status": "failed", "error": str(exc)})
        if canceled() and record.current_run_id:
            record.steps.append({"name": "child_cancellation", "status": "canceled", "run_id": record.current_run_id})
        record.current_run_id = None
        record.validation_executed = executed > 0
        if record.status in ACTIVE:
            if stopped:
                record.status = "canceled"
            elif not executed and all_passed:
                record.status = "completed"
                record.failure = {"kind": "no_validation_executed",
                                  "message": "No test profile or browser scenario ran, so nothing was validated. This is inspection evidence only."}
            else:
                record.status = "passed" if all_passed else "failed"
        if campaign_store:
            record = campaign_store.finalize(record)
    return record.to_dict()
