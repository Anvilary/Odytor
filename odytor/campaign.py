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

import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from odytor.browser import submit_browser_run, wait_for_run
from odytor.errors import GitPlusError, SecurityError, ValidationError
from odytor.github import GitHubClient
from odytor.git_workspace import pr_context
from odytor.jobs import JobService, new_run_id
from odytor.profile import PROFILE_NAME, PROFILES, REGISTRY, RepoPolicy
from odytor.run_tests import cancel_test_run, submit_workspace_run
from odytor.workspace import open_workspace

logger = logging.getLogger(__name__)


@dataclass
class CampaignRecord:
    campaign_id: str
    owner: str
    repo: str
    number: int
    mode: str
    status: str  # "running" | "passed" | "failed" | "setup_error" | "timeout" | "canceled"
    created_at: float
    updated_at: float
    finished_at: float | None = None
    tested_head: str | None = None
    tested_base: str | None = None
    tested_tree: str | None = None
    workspace_id: str | None = None
    current_run_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    browser: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    workspace_cleaned: bool = False
    github_mutations: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "owner": self.owner,
            "repo": self.repo,
            "number": self.number,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "tested_head": self.tested_head,
            "tested_base": self.tested_base,
            "tested_tree": self.tested_tree,
            "steps": self.steps,
            "results": self.results,
            "browser": self.browser,
            "artifacts": self.artifacts,
            "failure": self.failure,
            "workspace_cleaned": self.workspace_cleaned,
            "github_mutations": "none",
        }


class CampaignStore:
    """Thread-safe persistence for validation campaigns."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, campaign_id: str) -> Path:
        return self.root / f"{campaign_id}.json"

    def save(self, record: CampaignRecord) -> None:
        p = self._path(record.campaign_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(p)

    def load(self, campaign_id: str, owner: str) -> CampaignRecord | None:
        p = self._path(campaign_id)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("owner") != owner:
                return None
            return CampaignRecord(**data)
        except Exception:
            return None

    def cancel(self, campaign_id: str, owner: str, *, service: JobService, workspaces: Any) -> dict[str, Any]:
        record = self.load(campaign_id, owner)
        if record is None:
            raise ValidationError(f"Unknown campaign '{campaign_id}'.")
        if record.status not in {"running"}:
            return {"campaign_id": campaign_id, "status": record.status, "already_finished": True}

        record.status = "canceled"
        record.finished_at = time.time()
        record.updated_at = time.time()
        record.failure = {"kind": "canceled", "message": "Campaign canceled by caller."}

        # Cancel active runner child job if running
        if record.current_run_id:
            try:
                cancel_test_run(record.current_run_id, service=service, owner=owner)
            except Exception as exc:
                logger.warning("Failed to cancel child run %s: %s", record.current_run_id, exc)

        # Ensure disposable workspace is cleaned
        if record.workspace_id and not record.workspace_cleaned:
            try:
                workspaces.close(record.workspace_id, owner)
                record.workspace_cleaned = True
            except Exception as exc:
                logger.warning("Failed to close workspace %s on cancellation: %s", record.workspace_id, exc)

        self.save(record)
        return {"campaign_id": campaign_id, "status": "canceled", "workspace_cleaned": record.workspace_cleaned}


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
    overlay_patch: str | None = None,
    timeout_seconds: int = 300,
    workspaces: Any,
    service: JobService,
    sessions: Any,
    policies: dict[str, RepoPolicy],
    owner: str,
    cfg: Any,
    campaign_store: CampaignStore | None = None,
) -> dict[str, Any]:
    """Execute the bounded disposable validation campaign."""
    now = time.time()
    campaign_id = f"cmp-{secrets.token_hex(16)}"
    requested_profiles = profiles or ["python-pytest"]

    # Validate profiles
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

    # PR Context and revision pinning
    ctx = pr_context(client, repo, number, expected_head=expected_head, write=False)
    live_head = ctx["head_sha"]
    live_base = ctx["base_sha"]

    if expected_head and expected_head.lower() != live_head.lower():
        raise ValidationError(f"PR head has moved: expected {expected_head}, found {live_head}.")

    if mode == "prospective_merge" and not expected_base:
        raise ValidationError("expected_base (the live target-branch tip) is required for mode='prospective_merge'.")

    record = CampaignRecord(
        campaign_id=campaign_id,
        owner=owner,
        repo=repo,
        number=number,
        mode=mode,
        status="running",
        created_at=now,
        updated_at=now,
        tested_head=live_head,
        tested_base=expected_base if mode == "prospective_merge" else None,
    )
    if campaign_store:
        campaign_store.save(record)

    ws_id = None
    all_passed = True
    deadline = now + timeout_seconds

    try:
        # Step 1: Create disposable workspace (NO 'publish' operation in scope)
        step_t0 = time.monotonic()
        ws_source = "pr_head" if mode == "pr_head" else "prospective_merge"
        ws_kwargs: dict[str, Any] = {
            "source": ws_source,
            "number": number,
            "expected_head": live_head,
            "operations": ["read", "search", "edit", "diff", "execute", "artifacts", "manage"],
        }
        if mode == "prospective_merge":
            ws_kwargs["expected_base"] = expected_base

        ws = open_workspace(client, repo, service=workspaces, owner=owner, **ws_kwargs)
        ws_id = ws["workspace_id"]
        expected_version = ws["version"]
        record.workspace_id = ws_id
        record.tested_tree = ws["tree"]
        record.steps.append({
            "name": "workspace_create",
            "status": "passed",
            "duration_seconds": round(time.monotonic() - step_t0, 3),
            "workspace_id": ws_id,
            "tree": ws["tree"],
        })
        if campaign_store:
            campaign_store.save(record)

        # Step 2: Apply overlay patch if supplied
        if overlay_patch:
            step_t0 = time.monotonic()
            patch_res = workspaces.apply_patch(ws_id, owner, expected_version=expected_version, patch=overlay_patch)
            expected_version = patch_res["version"]
            record.tested_tree = patch_res["tree"]
            record.steps.append({
                "name": "apply_overlay",
                "status": "passed",
                "duration_seconds": round(time.monotonic() - step_t0, 3),
                "patch_bytes": len(overlay_patch),
                "new_tree": patch_res["tree"],
            })
            if campaign_store:
                campaign_store.save(record)

        # Step 3: Run execution profiles sequentially
        for prof in requested_profiles:
            if time.time() > deadline:
                raise TimeoutError("Validation campaign exceeded total execution timeout.")

            step_t0 = time.monotonic()
            selectors = (profile_selectors or {}).get(prof)
            remaining_time = max(deadline - time.time(), 10)

            sub = submit_workspace_run(
                repo,
                ws_id,
                expected_version,
                workspaces=workspaces,
                service=service,
                policies=policies,
                owner=owner,
                cfg=cfg.testing,
                profile=prof,
                selectors=selectors,
            )
            run_id = sub["run_id"]
            record.current_run_id = run_id
            if campaign_store:
                campaign_store.save(record)

            final = wait_for_run(service, run_id, remaining_time)
            dur = round(time.monotonic() - step_t0, 3)

            if final is None:
                record.steps.append({"name": f"profile:{prof}", "status": "timeout", "duration_seconds": dur, "run_id": run_id})
                record.results[prof] = {"status": "timeout", "run_id": run_id}
                all_passed = False
                break

            status = final.get("status", "unknown")
            record.steps.append({
                "name": f"profile:{prof}",
                "status": status,
                "duration_seconds": dur,
                "run_id": run_id,
                "failure": final.get("failure"),
            })
            record.results[prof] = {
                "status": status,
                "run_id": run_id,
                "duration_seconds": dur,
                "failure": final.get("failure"),
                "counts": final.get("counts"),
                "diagnostics_summary": final.get("diagnostics_summary"),
            }
            # Collect artifacts from run
            for art in final.get("artifacts") or []:
                record.artifacts.append(art)

            if status != "passed":
                all_passed = False
                # If failure or error, halt further profiles
                break

            if campaign_store:
                campaign_store.save(record)

        # Step 4: Run browser scenario if requested and all tests passed (or if only browser was requested)
        if browser_scenario and all_passed:
            if time.time() > deadline:
                raise TimeoutError("Validation campaign exceeded total execution timeout before browser run.")

            step_t0 = time.monotonic()
            remaining_time = max(deadline - time.time(), 15)

            sub_b = submit_browser_run(
                repo,
                ws_id,
                expected_version,
                browser_scenario,
                workspaces=workspaces,
                sessions=sessions,
                service=service,
                policies=policies,
                owner=owner,
                cfg=cfg.testing,
                mocks=browser_mocks,
                video=browser_video,
            )
            b_run_id = sub_b["run_id"]
            record.current_run_id = b_run_id
            if campaign_store:
                campaign_store.save(record)

            final_b = wait_for_run(service, b_run_id, remaining_time)
            dur = round(time.monotonic() - step_t0, 3)

            if final_b is None:
                record.steps.append({"name": "browser-playwright", "status": "timeout", "duration_seconds": dur, "run_id": b_run_id})
                record.browser = {"status": "timeout", "run_id": b_run_id}
                all_passed = False
            else:
                b_status = final_b.get("status", "unknown")
                record.steps.append({
                    "name": "browser-playwright",
                    "status": b_status,
                    "duration_seconds": dur,
                    "run_id": b_run_id,
                    "failure": final_b.get("failure"),
                })
                record.browser = {
                    "status": b_status,
                    "run_id": b_run_id,
                    "duration_seconds": dur,
                    "counts": final_b.get("counts"),
                    "network": final_b.get("network"),
                    "failure": final_b.get("failure"),
                }
                for art in final_b.get("artifacts") or []:
                    record.artifacts.append(art)

                if b_status != "passed":
                    all_passed = False

            if campaign_store:
                campaign_store.save(record)

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
        # Step 5: Clean up disposable workspace (guaranteed under all outcomes)
        if ws_id:
            try:
                workspaces.close(ws_id, owner)
                record.workspace_cleaned = True
                record.steps.append({"name": "workspace_cleanup", "status": "passed", "closed": True})
            except Exception as exc:
                logger.error("Failed to dispose workspace %s: %s", ws_id, exc)
                record.steps.append({"name": "workspace_cleanup", "status": "failed", "error": str(exc)})

        record.finished_at = time.time()
        record.updated_at = time.time()
        record.current_run_id = None
        if record.status == "running":
            record.status = "passed" if all_passed else "failed"

        if campaign_store:
            campaign_store.save(record)

    return record.to_dict()
