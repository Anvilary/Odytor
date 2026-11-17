from unittest.mock import MagicMock

import pytest

from git_plus.ci_control import control_ci
from git_plus.errors import APIError, RevisionMismatchError, SecurityError, ValidationError
from git_plus.github import GitHubClient

SHA = "a" * 40
FORK_SHA = "b" * 40


def response(data):
    obj = MagicMock()
    obj.json.return_value = data
    return obj


def setup_client(*, fork=False):
    client = MagicMock(spec=GitHubClient)
    client.token = "token"
    head_repo = "fork/project" if fork else "owner/project"
    client.get_pr.return_value = {
        "head": {"sha": SHA, "repo": {"full_name": head_repo}, "ref": "topic"},
        "base": {"sha": "c" * 40, "repo": {"full_name": "owner/project"}, "ref": "main"},
        "state": "open", "maintainer_can_modify": False,
    }
    return client, head_repo


def run_data(head_repo="owner/project", **overrides):
    return {"id": 10, "head_sha": SHA, "head_repository": {"full_name": head_repo},
            "event": "pull_request", "status": "completed", "conclusion": "failure",
            "run_attempt": 2, **overrides}


def test_rerun_failed_verifies_revision_and_attempt_before_post():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response(run_data()), response({})]
    result = control_ci(client, "owner/project", 4, SHA, "rerun_failed", run_id=10)
    assert result["accepted"] and result["completed"] is False
    assert [call.args[:2] for call in client.request.call_args_list] == [
        ("GET", "repos/owner/project/actions/runs/10"),
        ("GET", "repos/owner/project/actions/runs/10"),
        ("POST", "repos/owner/project/actions/runs/10/rerun-failed-jobs"),
    ]


@pytest.mark.parametrize("operation,extra,endpoint", [
    ("rerun_all", {}, "repos/owner/project/actions/runs/10/rerun"),
    ("approve_fork", {"user_authorized_fork_approval": True}, "repos/owner/project/actions/runs/10/approve"),
])
def test_run_controls(operation, extra, endpoint):
    client, _ = setup_client(fork=operation == "approve_fork")
    if operation == "approve_fork":
        client.request.side_effect = [response(run_data("fork/project", conclusion="action_required")),
                                      response(run_data("fork/project", conclusion="action_required")), response({})]
    else:
        client.request.side_effect = [response(run_data()), response(run_data()), response({})]
    control_ci(client, "owner/project", 4, SHA, operation, run_id=10, **extra)
    assert client.request.call_args_list[-1].args[:2] == ("POST", endpoint)


def test_rerun_job_verifies_job_run_and_revision():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response({"id": 20, "run_id": 10, "head_sha": SHA,
                                                                  "status": "completed", "conclusion": "failure"}),
                                  response(run_data()), response({})]
    result = control_ci(client, "owner/project", 4, SHA, "rerun_job", run_id=10, job_id=20)
    assert result["accepted"]
    assert client.request.call_args_list[-1].args[1].endswith("/jobs/20/rerun")


def test_dispatch_reports_ref_race_and_resolved_revision():
    client, _ = setup_client()
    client.request.side_effect = [response({"id": 7, "state": "active"}), response({"object": {"sha": SHA}}),
                                   response({"id": 7, "state": "active"}), response({"object": {"sha": SHA}}), response({})]
    result = control_ci(client, "owner/project", 4, SHA, "dispatch", workflow_id=7,
                        ref="topic", inputs={}, authorize_dispatch_semantics=True)
    assert result["resolved_ref_sha"] == SHA and result["exact_revision_guaranteed"] is False
    assert client.request.call_args_list[-1].kwargs["json_data"] == {"ref": "topic", "inputs": {}}


def test_rejects_stale_run_and_attempt_drift():
    client, _ = setup_client()
    client.request.return_value = response(run_data(head_sha=FORK_SHA))
    with pytest.raises(RevisionMismatchError):
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10)
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response(run_data(run_attempt=3))]
    with pytest.raises(ValidationError, match="run changed"):
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10)


def test_rejects_bad_arguments_and_unapproved_fork():
    client, _ = setup_client()
    with pytest.raises(ValidationError):
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=0)
    with pytest.raises(ValidationError):
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10, job_id=20)
    client, _ = setup_client(fork=True)
    client.request.return_value = response(run_data("fork/project", conclusion="action_required"))
    with pytest.raises(SecurityError, match="explicit user authorization"):
        control_ci(client, "owner/project", 4, SHA, "approve_fork", run_id=10)


def test_propagates_api_permission_failure_without_retrying_mutation():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response(run_data()), APIError("forbidden", status_code=403)]
    with pytest.raises(APIError, match="forbidden"):
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10)
    assert client.request.call_args_list[-1].args[0] == "POST"


def test_dispatch_rejects_unrelated_ref_and_stale_ref():
    client, _ = setup_client()
    with pytest.raises(ValidationError, match="PR head ref"):
        control_ci(client, "owner/project", 4, SHA, "dispatch", workflow_id=7,
                   ref="other", inputs={}, authorize_dispatch_semantics=True)
    client, _ = setup_client()
    client.request.side_effect = [response({"id": 7, "state": "active"}), response({"object": {"sha": FORK_SHA}})]
    with pytest.raises(RevisionMismatchError):
        control_ci(client, "owner/project", 4, SHA, "dispatch", workflow_id=7,
                   ref="topic", inputs={}, authorize_dispatch_semantics=True)


def test_dispatch_rejects_closed_pr_and_nested_inputs():
    client, _ = setup_client()
    client.get_pr.return_value["state"] = "closed"
    with pytest.raises(ValidationError, match="open, unmerged"):
        control_ci(client, "owner/project", 4, SHA, "dispatch", workflow_id=7,
                   ref="topic", inputs={}, authorize_dispatch_semantics=True)
    client, _ = setup_client()
    with pytest.raises(ValidationError, match="strings"):
        control_ci(client, "owner/project", 4, SHA, "dispatch", workflow_id=7,
                   ref="topic", inputs={"x": {"nested": 1}}, authorize_dispatch_semantics=True)


def test_rerun_all_accepts_success_but_rerun_failed_rejects_it():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data(conclusion="success")), response(run_data(conclusion="success")), response({})]
    assert control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10)["accepted"]
    client, _ = setup_client()
    client.request.return_value = response(run_data(conclusion="success"))
    with pytest.raises(ValidationError, match="no failed jobs"):
        control_ci(client, "owner/project", 4, SHA, "rerun_failed", run_id=10)


def test_job_requires_exact_run_and_failed_state():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response({"id": 20, "run_id": 11, "head_sha": SHA,
                                                                  "status": "completed", "conclusion": "failure"})]
    with pytest.raises(ValidationError, match="does not belong"):
        control_ci(client, "owner/project", 4, SHA, "rerun_job", run_id=10, job_id=20)


def test_unknown_mutation_api_error_is_marked_ambiguous():
    client, _ = setup_client()
    client.request.side_effect = [response(run_data()), response(run_data()), APIError("server", status_code=503)]
    with pytest.raises(APIError, match="outcome is unknown") as caught:
        control_ci(client, "owner/project", 4, SHA, "rerun_all", run_id=10)
    assert caught.value.diagnostic["classification"] == "mutation_outcome_unknown"
