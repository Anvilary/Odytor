"""test_run / test_result: exact-revision preparation on the trusted side, execution elsewhere.

Git Plus fetches, merges, applies the optional overlay and exports a credential-free tree
artifact. Only that artifact and a validated job description cross to the isolated runner.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

from git_plus.config import RunSettings
from git_plus.errors import APIError, GitPlusError, RevisionMismatchError, SecurityError, ValidationError
from git_plus.git_workspace import GitWorkspace, pr_context, verify_context
from git_plus.github import GitHubClient, validate_repo, validate_sha
from git_plus.jobs import TERMINAL, JobService, QueuedJob, new_run_id
from git_plus.pr_conflicts import inspect_merge
from git_plus.profile import (PROFILE_NAME, PROFILES, ProfileError, RepoPolicy, validate_selectors)

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
    permissions = client.request("GET", f"repos/{repo}").json().get("permissions") or {}
    if not any(permissions.get(level) is True for level in ("pull", "triage", "push", "maintain", "admin")):
        raise SecurityError("The authenticated GitHub user lacks read access to this repository.")


def submit_test_run(client: GitHubClient, repo: str, number: int, expected_head: str, *,
                    service: JobService, policies: dict[str, RepoPolicy], owner: str, cfg: RunSettings,
                    profile: str = PROFILE_NAME, selectors: Any = None, mode: str = "head",
                    expected_base: str | None = None, test_overlay: str | None = None) -> dict[str, Any]:
    policy = policies.get(repo.strip().casefold())
    profile, selected = _validate_request(repo, number, expected_head, profile, selectors, mode, expected_base, policy)
    if test_overlay is not None:
        raise ValidationError("Test overlays are not supported by this build.")
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
            overlay = {"supplied": False, "applied": False, "sha256": None, "paths": [], "patch_status": "none"}
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
            trees["effective_tree"] = effective
            record["artifact"] = work.export_tree(effective, spool / "source.tar", max_bytes=cfg.max_source_bytes)
            verify_context(client, repo, number, context)
        header = {"schema": 1, "job_id": run_id, "repo": repo, "profile": profile, "profile_version": PROFILES[profile],
                  "test_dirs": list(policy.test_dirs), "selectors": list(selected),
                  "requirements": policy.requirements, "timeout_seconds": cfg.default_timeout}
        service.enqueue(record, QueuedJob(run_id, spool, header, cfg.default_timeout))
        reserved = False
        return public_view(record)
    finally:
        if reserved:
            service.release()
            if spool is not None:
                shutil.rmtree(spool, ignore_errors=True)


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


def public_view(record: dict[str, Any], currency: dict[str, Any] | None = None) -> dict[str, Any]:
    ts = record["timestamps"]
    result = record.get("result") or {}
    status = record["status"]
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
        "tests_ran_against": "reviewer_overlay_applied_to_pr_tree" if record["overlay"].get("applied") else "unmodified_pr_tree",
        "github_mutations": "none",
    }
    if status in TERMINAL:
        view.update({k: result.get(k) for k in ("failure", "counts", "tests", "tests_total", "tests_truncated",
                                                  "selector_coverage", "log", "execution", "runner", "environment",
                                                  "reports", "conflict", "storage") if k in result})
        view["green"] = status == "passed"
        view["evidence_complete"] = status in {"passed", "failed", "no_tests_executed", "no_tests_collected"} and bool(result.get("counts") is not None)
    else:
        view["green"] = False
        view["evidence_complete"] = False
    if currency is not None:
        view["currency"] = currency
    return view


def fetch_test_result(client: GitHubClient, repo: str, run_id: str, *, service: JobService, owner: str) -> dict[str, Any]:
    record = service.get(run_id)
    # One indistinguishable error for unknown ids and other callers' runs.
    if record is None or record["owner"] != owner or record["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown run_id.")
    currency = _currency(client, record)
    if currency["pr_state"] in {"closed", "merged"}:
        grace = service.clock() + service.cfg.closed_grace_hours * 3600
        for item in service.store.records():
            if item["repo"].casefold() == repo.casefold() and item["pr"] == record["pr"]:
                service.mutate(item["run_id"], lambda r: r.__setitem__("expires_at", min(r["expires_at"], grace)))
        record = service.store.load(run_id) or record
    return public_view(record, currency)
