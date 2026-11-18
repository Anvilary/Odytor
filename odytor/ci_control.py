"""Revision-aware controls for GitHub Actions runs."""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from odytor.errors import APIError, RevisionMismatchError, SecurityError, ValidationError
from odytor.git_workspace import pr_context, validate_ref, verify_context
from odytor.github import GitHubClient, validate_repo, validate_sha

Operation = Literal["rerun_failed", "rerun_all", "rerun_job", "dispatch", "approve_fork"]


def control_ci(
    client: GitHubClient,
    repo: str,
    number: int,
    expected_head: str,
    operation: Operation,
    *,
    run_id: int | None = None,
    job_id: int | None = None,
    workflow_id: int | None = None,
    ref: str | None = None,
    inputs: dict[str, Any] | None = None,
    authorize_dispatch_semantics: bool = False,
    user_authorized_fork_approval: bool = False,
) -> dict[str, Any]:
    """Request one Actions operation after tying it to the requested PR revision."""
    validate_repo(repo)
    expected_head = validate_sha(expected_head, "expected_head")
    if type(number) is not int or number <= 0:
        raise ValidationError("PR number must be positive.")
    if not client.token:
        raise SecurityError("GitHub authentication is required for Actions control.")
    if operation not in {"rerun_failed", "rerun_all", "rerun_job", "dispatch", "approve_fork"}:
        raise ValidationError("Unsupported CI operation.")

    def require_shape(*, needs_run: bool = False, needs_job: bool = False,
                      needs_workflow: bool = False, needs_ref: bool = False,
                      needs_inputs: bool = False) -> None:
        expected = {"run_id": needs_run, "job_id": needs_job,
                    "workflow_id": needs_workflow, "ref": needs_ref,
                    "inputs": needs_inputs}
        supplied = {"run_id": run_id is not None, "job_id": job_id is not None,
                    "workflow_id": workflow_id is not None, "ref": ref is not None,
                    "inputs": inputs is not None}
        if expected != supplied:
            raise ValidationError("CI operation received missing or irrelevant arguments.")
        for label, value in (("run_id", run_id), ("job_id", job_id), ("workflow_id", workflow_id)):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValidationError(f"{label} must be a positive integer.")

    if operation == "dispatch":
        require_shape(needs_workflow=True, needs_ref=True, needs_inputs=True)
        if authorize_dispatch_semantics is not True:
            raise SecurityError("Dispatch requires explicit acknowledgment that it is not a normal PR check.")
        assert workflow_id is not None and ref is not None and inputs is not None
        ref = validate_ref(ref)
    else:
        require_shape(needs_run=True, needs_job=operation == "rerun_job")
        if authorize_dispatch_semantics:
            raise ValidationError("Dispatch acknowledgment is irrelevant to this operation.")
    if operation != "approve_fork" and user_authorized_fork_approval:
        raise ValidationError("Fork approval authorization is irrelevant to this operation.")

    context = pr_context(client, repo, number, expected_head, write=False)
    base = f"repos/{repo}/actions"

    if operation == "dispatch":
        assert workflow_id is not None and ref is not None and inputs is not None
        pr = client.get_pr(repo, number)
        if pr.get("state") != "open" or pr.get("merged") is True:
            raise ValidationError("Dispatch requires an open, unmerged PR.")
        if ref != context["head_ref"]:
            raise ValidationError("Dispatch ref must be the requested PR head ref.")
        if not isinstance(inputs, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str)
                                               for k, v in inputs.items()):
            raise ValidationError("Dispatch inputs must map non-empty names to strings.")
        target_base = f"repos/{context['head_repo']}/actions"
        workflow = client.request("GET", f"{target_base}/workflows/{workflow_id}").json()
        if workflow.get("id") != workflow_id or workflow.get("state") != "active":
            raise ValidationError("Workflow must exist and be active.")
        target = client.request("GET", f"repos/{context['head_repo']}/git/ref/heads/{quote(ref, safe='/')}").json()
        resolved = validate_sha(((target.get("object") or {}).get("sha", "")), "dispatch ref SHA")
        if resolved != expected_head:
            raise RevisionMismatchError(expected_head, resolved, "Dispatch ref does not resolve to the requested PR revision.")
        fresh = pr_context(client, repo, number, expected_head, write=False)
        latest_workflow = client.request("GET", f"{target_base}/workflows/{workflow_id}").json()
        latest_ref = client.request("GET", f"repos/{fresh['head_repo']}/git/ref/heads/{quote(ref, safe='/')}").json()
        latest_sha = validate_sha(((latest_ref.get("object") or {}).get("sha", "")), "dispatch ref SHA")
        if fresh["head_repo"] != context["head_repo"] or fresh["head_ref"] != ref:
            raise RevisionMismatchError(expected_head, fresh["head_sha"], "PR target changed before dispatch.")
        if latest_workflow.get("id") != workflow_id or latest_workflow.get("state") != "active":
            raise ValidationError("Workflow is no longer active.")
        if latest_sha != expected_head:
            raise RevisionMismatchError(expected_head, latest_sha, "Dispatch ref moved before dispatch.")
        try:
            client.request("POST", f"{target_base}/workflows/{workflow_id}/dispatches",
                           json_data={"ref": ref, "inputs": inputs})
        except APIError as exc:
            _raise_mutation_outcome(exc)
        return {"operation": operation, "requested": True, "accepted": True,
                "repo": context["head_repo"], "ref": ref, "resolved_ref_sha": resolved,
                "expected_head": expected_head, "exact_revision_guaranteed": False,
                "warning": "GitHub dispatch targets a ref; that ref may move after resolution."}

    assert run_id is not None
    run = client.request("GET", f"{base}/runs/{run_id}").json()
    _validate_run(run, run_id, expected_head, context["head_repo"], number, repo, context)
    head_repo = (run.get("head_repository") or {}).get("full_name")
    if operation == "approve_fork":
        if user_authorized_fork_approval is not True:
            raise SecurityError("Fork workflow approval requires explicit user authorization.")
        if not context["is_fork"] or not (run.get("status") == "action_required" or (run.get("status") == "completed" and run.get("conclusion") == "action_required")):
            raise ValidationError("Only a completed action_required run from an actual fork can be approved.")
    elif user_authorized_fork_approval:
        raise ValidationError("Fork approval authorization is irrelevant to this operation.")
    elif run.get("status") != "completed":
        raise ValidationError("Only completed runs can be rerun.")
    elif operation == "rerun_failed" and run.get("conclusion") not in {"failure", "timed_out", "startup_failure"}:
        raise ValidationError("Run has no failed jobs eligible for rerun.")

    if operation == "rerun_job":
        assert job_id is not None
        job = client.request("GET", f"{base}/jobs/{job_id}").json()
        if job.get("id") != job_id or job.get("run_id") != run_id:
            raise ValidationError("Job does not belong to the requested run.")
        job_head = validate_sha(job.get("head_sha", ""), "job head SHA")
        if job_head != expected_head:
            raise RevisionMismatchError(expected_head, job_head, "Job is for a different PR revision.")
        if job.get("status") != "completed" or job.get("conclusion") not in {"failure", "timed_out"}:
            raise ValidationError("Only completed failed jobs can be rerun.")

    latest = client.request("GET", f"{base}/runs/{run_id}").json()
    _validate_run(latest, run_id, expected_head, context["head_repo"], number, repo, context)
    if latest.get("run_attempt") != run.get("run_attempt") or latest.get("status") != run.get("status") or latest.get("conclusion") != run.get("conclusion"):
        raise ValidationError("Actions run changed before the requested operation.")
    verify_context(client, repo, number, context)
    endpoints = {"rerun_failed": f"{base}/runs/{run_id}/rerun-failed-jobs",
                 "rerun_all": f"{base}/runs/{run_id}/rerun",
                 "rerun_job": f"{base}/jobs/{job_id}/rerun",
                 "approve_fork": f"{base}/runs/{run_id}/approve"}
    try:
        client.request("POST", endpoints[operation])
    except APIError as exc:
        _raise_mutation_outcome(exc)
    return {"operation": operation, "requested": True, "accepted": True,
            "repo": repo, "run_id": run_id, "run_attempt": latest.get("run_attempt"),
            "head_repo": head_repo, "expected_head": expected_head,
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "completed": False}


def _validate_run(run: dict[str, Any], run_id: int, expected_head: str, head_repo: str,
                  number: int, repo: str, context: dict[str, Any]) -> None:
    if run.get("id") != run_id:
        raise ValidationError("GitHub returned a different Actions run ID.")
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt <= 0:
        raise ValidationError("Actions run has an invalid run attempt.")
    actual = validate_sha(run.get("head_sha", ""), "run head SHA")
    if actual != expected_head:
        raise RevisionMismatchError(expected_head, actual, "Actions run is for a different PR revision.")
    actual_repo = (run.get("head_repository") or {}).get("full_name")
    if not actual_repo or actual_repo.casefold() != head_repo.casefold():
        raise SecurityError("Actions run head repository does not match the PR head repository.")
    if run.get("event") != "pull_request":
        raise SecurityError("Actions run is not a pull_request event.")
    _verify_pr_association(run.get("pull_requests"), number, repo, context)


def _verify_pr_association(linked: Any, number: int, repo: str, context: dict[str, Any]) -> None:
    """Tie the run to exactly the requested PR in exactly the requested repository.

    Repository identity is checked against the PR as GitHub reports it for the requested repository, never
    against tool arguments alone: the numeric repository id when both sides carry one, otherwise the complete
    owner/name. GitHub's workflow-run payload may carry either (or both); an entry that carries neither, that
    contradicts itself, or that names another repository or PR is refused.
    """
    if not linked:
        # GitHub omits the association for runs from forks; a same-repository PR's run must carry it.
        if context["is_fork"]:
            return
        raise SecurityError("Actions run has no pull request association (GitHub lists only open pull requests, and omits it for fork runs).")
    if not isinstance(linked, list) or len(linked) != 1 or not isinstance(linked[0], dict):
        raise SecurityError("Actions run is associated with several pull requests; the association is ambiguous.")
    entry = linked[0]
    if entry.get("number") != number:
        raise SecurityError("Actions run is linked to a different pull request.")
    base_repo = (entry.get("base") or {}).get("repo")
    if not isinstance(base_repo, dict):
        raise SecurityError("Actions run pull request association has no verifiable repository identity.")
    verified = False
    repo_id, trusted_id = base_repo.get("id"), context.get("base_repo_id")
    if repo_id is not None and trusted_id is not None:
        if type(repo_id) is not int or repo_id != trusted_id:
            raise SecurityError("Actions run is linked to a pull request in a different repository.")
        verified = True
    name = base_repo.get("full_name")
    if name is not None:
        if not isinstance(name, str) or name.casefold() != repo.casefold():
            raise SecurityError("Actions run is linked to a pull request in a different repository.")
        verified = True
    if not verified:
        raise SecurityError("Actions run pull request association has no verifiable repository identity.")


def _raise_mutation_outcome(exc: APIError) -> None:
    if exc.status_code is None or exc.status_code >= 500 or exc.diagnostic.get("classification") == "network_error":
        raise APIError("Actions mutation outcome is unknown; check GitHub before retrying.",
                       diagnostic={"classification": "mutation_outcome_unknown"}) from exc
    raise exc
