"""test_run / test_result: exact-revision preparation on the trusted side, execution elsewhere.

Odytor fetches, merges, applies the optional overlay and exports a credential-free tree
artifact. Only that artifact and a validated job description cross to the isolated runner.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

from odytor.config import RunSettings
from odytor.errors import APIError, GitPlusError, RevisionMismatchError, SecurityError, ValidationError
from odytor.git_workspace import GitWorkspace, pr_context, verify_context
from odytor.github import GitHubClient, validate_repo, validate_sha
from odytor.jobs import TERMINAL, JobService, QueuedJob, new_run_id
from odytor.overlay import apply_overlay, validate_overlay
from odytor.pr_conflicts import inspect_merge
from odytor.profile import (NODE_PROFILE, PLAYWRIGHT_SPEC_PROFILE, PROFILE_NAME, PROFILES, REGISTRY, ProfileError, RepoPolicy, validate_node_selectors,
                            validate_playwright_spec_selectors, validate_selectors)

MODES = ("head", "prospective_merge")
SCHEMA = 1


def _iso(epoch: float | None) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds") if epoch else None


def _validate_request(repo: str, number: int, expected_head: str, profile: str, selectors: Any, mode: str,
                      expected_base: str | None, policy: RepoPolicy | None) -> tuple[str, tuple[str, ...]]:
    validate_repo(repo)
    validate_sha(expected_head, "expected_head")
    if type(number) is not int or number <= 0:
        raise ValidationError("PR number must be positive.")
    if profile not in PROFILES:
        raise ValidationError(f"Unknown test profile '{profile}'. Available: {', '.join(PROFILES)}.")
    if mode not in MODES:
        raise ValidationError(f"mode must be one of: {', '.join(MODES)}.")
    if mode == "prospective_merge":
        if expected_base is None:
            raise ValidationError("prospective_merge requires expected_base.")
        validate_sha(expected_base, "expected_base")
    elif expected_base is not None:
        raise ValidationError("expected_base is only valid with mode='prospective_merge'.")
    if policy is None:
        raise SecurityError("This repository has no maintainer test policy and cannot be tested.")
    if policy.profile != profile:
        raise ValidationError(f"Repository policy requires profile '{policy.profile}'.")
    try:
        return profile, validate_selectors(selectors, policy.test_dirs)
    except ProfileError as exc:
        raise ValidationError(str(exc)) from exc


def _require_pull_access(client: GitHubClient, repo: str) -> None:
    data = client.request("GET", f"repos/{repo}").json()
    if data.get("private") is False:
        return
    permissions = data.get("permissions") or {}
    if not any(permissions.get(level) is True for level in ("pull", "triage", "push", "maintain", "admin")):
        raise SecurityError("The authenticated GitHub user lacks read access to this repository.")


def submit_test_run(client: GitHubClient, repo: str, number: int, expected_head: str, *,
                    service: JobService, policies: dict[str, RepoPolicy], owner: str, cfg: RunSettings,
                    profile: str = PROFILE_NAME, selectors: Any = None, mode: str = "head",
                    expected_base: str | None = None, test_overlay: str | None = None) -> dict[str, Any]:
    policy = policies.get(repo.strip().casefold())
    profile, selected = _validate_request(repo, number, expected_head, profile, selectors, mode, expected_base, policy)
    summary = None
    if test_overlay is not None:
        summary = validate_overlay(test_overlay, policy.test_dirs, max_bytes=cfg.max_overlay_bytes)
    expected_head = validate_sha(expected_head)
    expected_base = validate_sha(expected_base) if expected_base else None
    _require_pull_access(client, repo)
    context = pr_context(client, repo, number, expected_head)
    if context["state"] != "open" or context["merged"]:
        raise ValidationError("The pull request must be open to be tested.")
    service.reserve()
    run_id = new_run_id()
    reserved = True
    spool: Path | None = None
    try:
        spool = service.spool_dir(run_id)
        with GitWorkspace(client) as work:
            trees: dict[str, Any] = {}
            revisions: dict[str, Any] = {"head_sha": context["head_sha"], "head_repo": context["head_repo"],
                                         "head_ref": context["head_ref"], "base_ref": context["base_ref"]}
            if mode == "head":
                work.fetch(context["head_repo"], context["head_ref"], context["head_sha"], "head", depth=1)
                trees["pr_tree"] = work.git("rev-parse", f"{context['head_sha']}^{{tree}}").decode().strip()
                revisions.update(base_sha=context["base_sha"], base_sha_role="pr_api_snapshot_not_part_of_tested_tree")
                conflict = None
            else:
                work.fetch(context["head_repo"], context["head_ref"], context["head_sha"], "head")
                # The caller must name the live target-branch tip; GitHub's cached PR base snapshot is not consulted.
                work.fetch(context["base_repo"], context["base_ref"], expected_base, "base")
                merge = inspect_merge(work, expected_base, context["head_sha"], max_paths=20)
                revisions.update(base_sha=expected_base, base_sha_role="live_target_branch_tip",
                                 pr_api_base_snapshot=context["base_sha"])
                trees["pr_tree"] = work.git("rev-parse", f"{context['head_sha']}^{{tree}}").decode().strip()
                conflict = merge if merge["conflicting"] else None
                if not conflict:
                    trees["merge_tree"] = merge["merge_tree_sha"]
            overlay = {"supplied": False, "applied": False, "sha256": None, "paths": [], "patch_status": "none",
                       "committed": False, "pushed": False}
            now = service.clock()
            record: dict[str, Any] = {
                "schema": SCHEMA, "run_id": run_id, "owner": owner, "repo": repo, "pr": number, "mode": mode,
                "profile": profile, "profile_version": PROFILES[profile], "status": "queued",
                "requested": {"head": expected_head, "base": expected_base},
                "revisions": revisions, "trees": trees, "overlay": overlay, "selectors": list(selected),
                "test_dirs": list(policy.test_dirs), "timeout_seconds": cfg.default_timeout,
                "requirements_sha256": policy.requirements_sha256,
                "timestamps": {"created_at": now, "started_at": None, "finished_at": None},
                "expires_at": now + cfg.retention_hours * 3600, "result": None}
            if conflict:
                record["status"] = "merge_conflict"
                record["result"] = {"status": "merge_conflict", "failure": {
                    "kind": "merge_conflict", "message": "The exact head and base cannot be merged; no tests were run."},
                    "conflict": {"total_conflicting_paths": conflict["total_conflicting_paths"],
                                 "paths": [c["path"] for c in conflict["conflicts"]],
                                 "paths_complete": conflict["paths_complete"]}}
                record["timestamps"]["finished_at"] = now
                service.store.save(record)
                return public_view(record)
            effective = trees.get("merge_tree") or trees["pr_tree"]
            if summary:
                effective = apply_overlay(work, effective, summary, test_overlay)
                overlay.update(supplied=True, applied=True, sha256=summary.sha256, bytes=summary.patch_bytes,
                               paths=list(summary.files), new_paths=list(summary.new_files),
                               test_paths=list(summary.test_files), added_lines=summary.added_lines,
                               removed_lines=summary.removed_lines, patch_status="applied_cleanly",
                               base_tree=trees.get("merge_tree") or trees["pr_tree"])
            trees["effective_tree"] = effective
            record["artifact"] = work.export_tree(effective, spool / "source.tar", max_bytes=cfg.max_source_bytes)
            verify_context(client, repo, number, context)
        header = {"schema": 1, "job_id": run_id, "repo": repo, "profile": profile, "profile_version": PROFILES[profile],
                  "test_dirs": list(policy.test_dirs), "selectors": list(selected),
                  "overlay_paths": list(summary.test_files) if summary else [],
                  "requirements": policy.requirements, "timeout_seconds": cfg.default_timeout}
        service.enqueue(record, QueuedJob(run_id, spool, header, cfg.default_timeout))
        reserved = False
        return public_view(record)
    finally:
        if reserved:
            service.release()
            if spool is not None:
                shutil.rmtree(spool, ignore_errors=True)


def workspace_playwright_selectors(policy: RepoPolicy, selectors: Any, test_title: str | None = None):
    table = policy.extra.get(PLAYWRIGHT_SPEC_PROFILE, {})
    specs = table.get("specs", ())
    if test_title:
        default = specs if len(specs) == 1 else ()
        selectors = [f"{s}::{test_title}" if "::" not in s else s for s in (selectors or default)]
    return validate_playwright_spec_selectors(selectors, allowed_specs=specs,
                                              allowed_dirs=table.get("spec_dirs", ()), test_dirs=policy.test_dirs)


def submit_workspace_run(repo: str, workspace_id: str, expected_version: int, *, workspaces: Any, service: JobService,
                         policies: dict[str, RepoPolicy], owner: str, cfg: RunSettings, profile: str = PROFILE_NAME,
                         selectors: Any = None, test_title: str | None = None) -> dict[str, Any]:
    """Queue an isolated pytest run on the exact current tree of a workspace.

    The tree is exported while the workspace lock is held and the job carries only that tar, so a later edit can
    never change what runs. Nothing is fetched, committed, pushed or posted.
    """
    validate_repo(repo)
    policy = policies.get(repo.strip().casefold())
    if policy is None:
        raise SecurityError("This repository has no maintainer test policy and cannot be tested.")
    if profile not in PROFILES:
        raise ValidationError(f"Unknown test profile '{profile}'. Available: {', '.join(PROFILES)}.")
    spec = REGISTRY[profile]
    if spec.kind == "browser":
        raise ValidationError("Browser scenarios run through browser_run, not workspace_run.")
    if profile not in policy.profiles:
        raise ValidationError(f"The maintainer policy for this repository enables: {', '.join(policy.profiles)}.")
    try:
        parsed_selectors = []
        if spec.kind == "node_test":
            test_dirs = policy.extra.get(NODE_PROFILE, {}).get("test_dirs", policy.test_dirs)
            selected = validate_node_selectors(selectors, test_dirs)
        elif spec.kind == "playwright_spec":
            cfg_table = policy.extra.get(PLAYWRIGHT_SPEC_PROFILE, {})
            selected, parsed_selectors = workspace_playwright_selectors(policy, selectors, test_title)
            test_dirs = cfg_table.get("spec_dirs", policy.test_dirs)
        elif spec.kind == "pytest":
            test_dirs = policy.test_dirs
            selected = validate_selectors(selectors, test_dirs)
        else:
            test_dirs = policy.test_dirs
            from odytor.profile import validate_path_selectors
            selected = validate_path_selectors(selectors)
    except ProfileError as exc:
        raise ValidationError(str(exc)) from exc
    current = workspaces.get(workspace_id, owner, operation="execute")
    if current["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown workspace_id.")
    service.reserve()
    run_id = new_run_id()
    reserved = True
    spool: Path | None = None
    try:
        spool = service.spool_dir(run_id)
        taken = workspaces.snapshot(workspace_id, owner, expected_version=expected_version, destination=spool / "source.tar",
                                    max_bytes=cfg.max_source_bytes, attach_run=run_id)
        ws = taken["record"]
        now = service.clock()
        record: dict[str, Any] = {
            "schema": SCHEMA, "kind": "workspace", "run_id": run_id, "owner": owner, "repo": repo, "pr": ws["pr"], "mode": "workspace",
            "profile": profile, "profile_version": PROFILES[profile], "status": "queued",
            "requested": {"head": None, "base": None},
            "revisions": {"workspace_id": workspace_id, "workspace_version": ws["version"], "source": ws["source"]},
            "trees": {"source_tree": ws["source_tree"], "effective_tree": ws["tree"]},
            "overlay": {"supplied": False, "applied": False, "sha256": None, "paths": [], "patch_status": "none",
                        "committed": False, "pushed": False},
            "selectors": list(selected), "test_dirs": list(test_dirs), "timeout_seconds": cfg.default_timeout,
            "requirements_sha256": policy.requirements_sha256 if spec.needs_env else "", "artifact": taken["artifact"],
            "timestamps": {"created_at": now, "started_at": None, "finished_at": None},
            "expires_at": now + cfg.retention_hours * 3600, "result": None}
        header = {"schema": 1, "job_id": run_id, "repo": repo, "profile": profile, "profile_version": PROFILES[profile],
                  "test_dirs": list(test_dirs), "selectors": list(selected), "overlay_paths": [],
                  "requirements": policy.requirements if spec.needs_env else "", "timeout_seconds": cfg.default_timeout}
        if spec.kind == "playwright_spec":
            cfg_table = policy.extra.get(PLAYWRIGHT_SPEC_PROFILE, {})
            pw_header = {
                "launcher": cfg_table.get("launcher", "tests/e2e/notes-qa/run_notes_qa.py"),
                "config": cfg_table.get("config", "tests/e2e/notes-qa/playwright.config.js"),
                "specs": list(cfg_table.get("specs", ())),
                "spec_dirs": list(cfg_table.get("spec_dirs", ())),
                "parsed_selectors": parsed_selectors,
            }
            header["playwright_spec"] = pw_header
            record["playwright_spec"] = pw_header
        service.enqueue(record, QueuedJob(run_id, spool, header, cfg.default_timeout))
        reserved = False
        return public_view(record)
    finally:
        if reserved:
            service.release()
            if spool is not None:
                shutil.rmtree(spool, ignore_errors=True)


def cancel_test_run(run_id: str, *, service: JobService, owner: str) -> dict[str, Any]:
    """Cancel queued or running work. A running job's sandbox is torn down by the runner when the connection drops."""
    record = service.get(run_id)
    if record is None or record["owner"] != owner:
        raise ValidationError("Unknown run_id.")
    if record["status"] in TERMINAL:
        return {"run_id": run_id, "outcome": "already_finished", "status": record["status"]}
    outcome = service.cancel(run_id)
    latest = service.store.load(run_id) or record
    return {"run_id": run_id, "outcome": outcome, "status": latest["status"],
            "note": "A running job stops once its sandbox is torn down; poll test_result for status 'canceled'."
            if outcome == "canceling" else None}


def _currency(client: GitHubClient, record: dict[str, Any]) -> dict[str, Any]:
    """Compare the tested head with the PR now; also proves the caller can still read the PR."""
    try:
        pr = client.get_pr(record["repo"], record["pr"])
    except APIError as exc:
        raise SecurityError("Repository access could not be verified; the result is withheld.") from exc
    head = ((pr.get("head") or {}).get("sha") or "").lower()
    tested = record["revisions"]["head_sha"]
    return {"checked": True, "pr_state": "merged" if pr.get("merged") else pr.get("state"),
            "tested_head_is_current_pr_head": head == tested, "current_pr_head": head}


def _workspace_currency(record: dict[str, Any], workspaces: Any | None) -> dict[str, Any]:
    """Compare the tested tree with the workspace now; a closed or expired workspace is reported, not guessed."""
    ref = record["revisions"]
    out: dict[str, Any] = {"checked": workspaces is not None, "workspace_id": ref["workspace_id"],
                           "tested_version": ref["workspace_version"], "tested_tree": record["trees"]["effective_tree"]}
    if workspaces is None:
        return out
    live = workspaces.peek(ref["workspace_id"], record["owner"])
    if live is None:
        return {**out, "workspace_state": "closed_or_expired", "tested_tree_is_current_workspace_tree": None}
    return {**out, "workspace_state": "open", "current_version": live["version"], "current_tree": live["tree"],
            "tested_tree_is_current_workspace_tree": live["tree"] == record["trees"]["effective_tree"]}


def public_view(record: dict[str, Any], currency: dict[str, Any] | None = None,
                progress: dict[str, Any] | None = None, max_log_bytes: int = 65536) -> dict[str, Any]:
    workspace_run = record.get("kind") in {"workspace", "browser"}
    ts = record["timestamps"]
    result = record.get("result") or {}
    status = record["status"]
    tested_source = "workspace_tree" if workspace_run else (
        "prospective_merge_tree" if record["mode"] == "prospective_merge" else "unmodified_pr_tree")
    if status == "merge_conflict":
        tested_source = None
    elif record["overlay"].get("applied") and not workspace_run:
        tested_source = ("reviewer_overlay_applied_to_prospective_merge_tree" if record["mode"] == "prospective_merge"
                         else "reviewer_overlay_applied_to_pr_tree")
    view: dict[str, Any] = {
        "run_id": record["run_id"], "status": status, "repo": record["repo"], "pr": record["pr"],
        "mode": record["mode"], "profile": record["profile"], "profile_version": record["profile_version"],
        "requested_head": record["requested"]["head"], "requested_base": record["requested"]["base"],
        "verified": record["revisions"], "trees": record["trees"], "overlay": record["overlay"],
        "selectors": record["selectors"],
        "timing": {"created_at": _iso(ts["created_at"]), "started_at": _iso(ts.get("started_at")),
                   "finished_at": _iso(ts.get("finished_at")), "duration_seconds": ts.get("duration_seconds"),
                   "queue_delay_seconds": (round(ts["started_at"] - ts["created_at"], 3) if ts.get("started_at") else None),
                   "result_expires_at": _iso(record.get("expires_at"))},
        "artifact": record.get("artifact"),
        "tests_ran_against": tested_source,
        "github_mutations": "none",
    }
    if workspace_run:
        view["kind"] = record["kind"]
        view["workspace"] = {**record["revisions"], "tested_tree": record["trees"]["effective_tree"],
                             "source_tree": record["trees"]["source_tree"]}
    if status in TERMINAL:
        view.update({k: result.get(k) for k in ("failure", "counts", "tests", "tests_total", "tests_truncated",
                                                  "selector_coverage", "overlay_coverage", "log", "execution", "runner",
                                                  "environment", "reports", "conflict", "storage", "steps", "steps_total",
                                                  "replayed_steps", "browser", "serve", "console", "page_errors", "page_errors_total",
                                                  "network", "snapshots", "events", "artifacts", "playwright", "server_log") if k in result})
        if "log" in view and isinstance(view["log"], dict):
            raw_log = view["log"]
            head = (raw_log.get("head") or "").encode("utf-8", "replace")
            tail = (raw_log.get("tail") or "").encode("utf-8", "replace")
            if len(head) + len(tail) > max_log_bytes:
                tail = tail[-max_log_bytes:] if max_log_bytes else b""
                view["log"] = {
                    **raw_log,
                    "head": head[:max_log_bytes - len(tail)].decode("utf-8", "ignore"),
                    "tail": tail.decode("utf-8", "ignore"),
                    "truncated": True,
                }
        view["green"] = status == "passed"
        failure_kind = (result.get("failure") or {}).get("kind")
        view["structured_outcomes_complete"] = (
            status in {"passed", "failed", "completed", "no_tests_executed", "no_tests_collected"}
            and result.get("counts") is not None
            and failure_kind not in {"incomplete_run", "killed_by_signal", "collection_failure", "unexpected_exit"}
            and not result.get("tests_truncated")
        )
        view["evidence_complete"] = view["structured_outcomes_complete"]
        if isinstance(view.get("log"), dict):
            view["log_complete"] = not view["log"].get("truncated")
    else:
        view["green"] = False
        view["evidence_complete"] = False
        if progress is not None:
            view["phase"] = progress.get("phase", status)
            view["active_tests"] = progress.get("active_tests", [])
            view["live_counts"] = progress.get("counts", {})
            view["last_heartbeat_ago_seconds"] = progress.get("last_heartbeat_ago_seconds")
            view["last_progress_ago_seconds"] = progress.get("last_progress_ago_seconds")
            view["events"] = progress.get("events", [])
            view["next_cursor"] = progress.get("next_cursor")
            view["has_more"] = progress.get("has_more", False)
            view["latest_event_id"] = progress.get("latest_event_id", 0)
            view["events_remaining"] = progress.get("events_remaining", 0)
            view["cursor_gap"] = progress.get("cursor_gap", False)
            view["cursor_expired"] = progress.get("cursor_expired", False)
    if progress is not None:
        view["live_progress"] = progress
    if currency is not None:
        view["currency"] = currency
    return view


def fetch_test_result(client: GitHubClient, repo: str, run_id: str, *, service: JobService, owner: str,
                      workspaces: Any | None = None, wait_seconds: float = 0.0, after_event_id: int = 0,
                      max_events: int = 50, max_log_bytes: int = 65536) -> dict[str, Any]:
    record, progress = service.get_progress_or_wait(run_id, owner, wait_seconds=wait_seconds,
                                                    after_event_id=after_event_id,
                                                    max_events=max_events)
    # One indistinguishable error for unknown ids and other callers' runs.
    if record is None or record["owner"] != owner or record["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown run_id.")
    if record.get("kind") in {"workspace", "browser"}:
        return public_view(record, _workspace_currency(record, workspaces), progress=progress, max_log_bytes=max_log_bytes)
    currency = _currency(client, record)
    if currency["pr_state"] in {"closed", "merged"}:
        grace = service.clock() + service.cfg.closed_grace_hours * 3600
        for item in service.store.records():
            if item["repo"].casefold() == repo.casefold() and item["pr"] == record["pr"]:
                service.mutate(item["run_id"], lambda r: r.__setitem__("expires_at", min(r["expires_at"], grace)))
        record = service.store.load(run_id) or record
    return public_view(record, currency, progress=progress, max_log_bytes=max_log_bytes)
