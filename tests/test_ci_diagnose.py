"""Tests for CI failure diagnosis, log extraction, and annotation correlation."""

import io
import zipfile
from unittest.mock import MagicMock
import pytest
from git_plus.ci_diagnose import (
    archive_logs,
    classify_diagnostic,
    diagnose_ci,
    extract_failure_excerpts,
)
from git_plus.errors import APIError, RevisionMismatchError, ValidationError
from git_plus.github import GitHubClient


def test_classify_diagnostic() -> None:
    assert classify_diagnostic("runner was lost") == "infrastructure_failure"
    assert classify_diagnostic("FAILED tests/test_foo.py::test_bar - AssertionError") == "test_failure"
    assert classify_diagnostic("mypy: error: Incompatible return value") == "type_check_failure"
    assert classify_diagnostic("ruff failed with 2 errors") == "lint_failure"
    assert classify_diagnostic("could not compile `my-crate` due to build error:") == "compiler_failure"
    assert classify_diagnostic("Random step message") == "step_failure"


def test_extract_failure_excerpts() -> None:
    log_content = (
        "2026-11-17T12:00:00Z Step 1: Init\n"
        "2026-11-17T12:00:01Z Running pytest\n"
        "2026-11-17T12:00:02Z tests/test_core.py:45: AssertionError: expected 1 got 2\n"
        "2026-11-17T12:00:03Z FAILED tests/test_core.py::test_calc\n"
        "2026-11-17T12:00:04Z ================== 1 failed ==================\n"
    )
    excerpts, categories = extract_failure_excerpts(log_content)
    assert "test_failure" in categories
    assert len(excerpts) > 0
    # Line 3 should have file coordinates
    file_excerpts = [e for e in excerpts if e.get("file") == "tests/test_core.py"]
    assert len(file_excerpts) > 0
    assert file_excerpts[0]["line"] == 45


def test_archive_logs_safe() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("job1.txt", "Log content for job 1")
        z.writestr("nested/job2.txt", "Log content for job 2")
    buf.seek(0)

    members = archive_logs(buf.getvalue())
    assert len(members) == 2
    assert members[0][0] == "job1.txt"
    assert members[0][1] == "Log content for job 1"


def test_archive_logs_rejects_unsafe_paths() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../dangerous.txt", "evil")
    buf.seek(0)

    with pytest.raises(ValidationError) as exc_info:
        archive_logs(buf.getvalue())
    assert "Unsafe file path" in str(exc_info.value)


def test_diagnose_ci_integration() -> None:
    mock_client = MagicMock(spec=GitHubClient)
    mock_client.get_pr.return_value = {
        "number": 10,
        "head": {"sha": "aaaa1111bbbb2222cccc3333dddd4444eeee5555", "ref": "fix-branch"},
    }
    mock_client.get_workflow_runs.return_value = {
        "workflow_runs": [
            {"id": 1001, "name": "CI", "conclusion": "failure", "head_sha": "aaaa1111bbbb2222cccc3333dddd4444eeee5555"}
        ]
    }
    mock_client.get_run_jobs.return_value = {
        "jobs": [
            {
                "id": 2001,
                "name": "Unit Tests",
                "conclusion": "failure",
                "status": "completed",
                "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/3001",
                "steps": [
                    {"name": "Run tests", "number": 3, "conclusion": "failure"}
                ],
            }
        ]
    }
    mock_client.get_job_logs.return_value = (
        "pytest started\n"
        "tests/test_foo.py:12: AssertionError: failure\n"
        "FAILED tests/test_foo.py::test_x\n"
    )
    mock_client.get_check_run_annotations.return_value = [
        {
            "path": "tests/test_foo.py",
            "start_line": 12,
            "end_line": 12,
            "annotation_level": "failure",
            "title": "Assertion Failure",
            "message": "assert False",
        }
    ]
    mock_client.get_code_scanning_alerts.return_value = []
    mock_client.request.return_value.json.return_value = []

    res = diagnose_ci(mock_client, "owner/repo", 10)
    assert res["number"] == 10
    assert res["total_failed_jobs"] == 1
    assert "test_failure" in res["failure_categories"]
    assert res["evidence_complete"] is True
    assert len(res["failed_jobs"][0]["annotations"]) == 1
    assert res["failed_jobs"][0]["annotations"][0]["path"] == "tests/test_foo.py"
    mock_client.request.assert_called_once_with(
        "GET", "repos/owner/repo/code-scanning/alerts",
        params={"per_page": 100, "ref": "refs/pull/10/head"},
    )


def _minimal_ci_client() -> MagicMock:
    client = MagicMock(spec=GitHubClient)
    client.get_pr.side_effect = [
        {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
        {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
    ]
    client.get_workflow_runs.return_value = {"workflow_runs": []}
    client.request.return_value.json.return_value = []
    return client


def test_diagnose_ci_excludes_run_with_different_head_sha() -> None:
    client = _minimal_ci_client()
    client.get_workflow_runs.return_value = {
        "workflow_runs": [{"id": 1, "head_sha": "c" * 40, "conclusion": "failure"}]
    }
    res = diagnose_ci(client, "owner/repo", 10)
    assert res["failed_jobs"] == []
    assert res["evidence_complete"] is False
    client.get_run_jobs.assert_not_called()


def test_diagnose_ci_selected_successful_job_is_not_a_failure() -> None:
    client = _minimal_ci_client()
    client.get_workflow_runs.return_value = {
        "workflow_runs": [{"id": 1, "head_sha": "a" * 40}]
    }
    client.get_run_jobs.return_value = {"jobs": [{"id": 2, "conclusion": "success"}]}
    res = diagnose_ci(client, "owner/repo", 10, job_id=2)
    assert res["failed_jobs"] == []
    assert res["total_failed_jobs"] == 0


def test_diagnose_ci_final_revision_drift_marks_incomplete() -> None:
    client = _minimal_ci_client()
    client.get_pr.side_effect = [
        {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
        {"head": {"sha": "c" * 40}, "base": {"sha": "d" * 40}},
    ]
    res = diagnose_ci(client, "owner/repo", 10)
    assert res["inconsistent_snapshot"] is True
    assert res["evidence_complete"] is False


@pytest.mark.parametrize("kind", ["runs", "jobs", "annotations"])
def test_diagnose_ci_page_limits_mark_evidence_incomplete(kind: str) -> None:
    client = _minimal_ci_client()
    if kind == "runs":
        client.get_workflow_runs.return_value = {
            "workflow_runs": [{"id": idx + 1, "head_sha": "a" * 40} for idx in range(30)],
            "total_count": 30,
        }
        client.get_run_jobs.return_value = {"jobs": []}
    else:
        client.get_workflow_runs.return_value = {
            "workflow_runs": [{"id": 1, "head_sha": "a" * 40}]
        }
        jobs = [{"id": idx, "conclusion": "success"} for idx in range(100)]
        if kind == "annotations":
            jobs = [{
                "id": 1, "conclusion": "failure",
                "check_run_url": "/check-runs/3",
            }]
            client.get_check_run_annotations.return_value = [{} for _ in range(100)]
            client.get_job_logs.return_value = "FAILED test_example"
        client.get_run_jobs.return_value = {"jobs": jobs, "total_count": len(jobs)}
    res = diagnose_ci(client, "owner/repo", 10)
    assert res["evidence_complete"] is False


def test_diagnose_ci_code_scanning_api_error_marks_incomplete() -> None:
    client = _minimal_ci_client()
    client.request.side_effect = APIError("Forbidden", status_code=403)
    res = diagnose_ci(client, "owner/repo", 10)
    assert res["evidence_complete"] is False


def test_diagnose_ci_code_scanning_alerts_require_captured_commit() -> None:
    client = _minimal_ci_client()
    client.request.return_value.json.return_value = [
        {"number": 1, "most_recent_instance": {"commit_sha": "a" * 40}},
        {"number": 2, "most_recent_instance": {"commit_sha": "c" * 40}},
    ]
    res = diagnose_ci(client, "owner/repo", 10)
    assert [a["number"] for a in res["code_scanning_alerts"]] == [1]
    assert res["evidence_complete"] is False


@pytest.mark.parametrize("field", ["run_id", "job_id"])
def test_diagnose_ci_missing_selected_id_marks_incomplete(field: str) -> None:
    client = _minimal_ci_client()
    kwargs = {field: 99}
    res = diagnose_ci(client, "owner/repo", 10, **kwargs)
    assert res["evidence_complete"] is False


@pytest.mark.parametrize("field", ["run_id", "job_id"])
def test_diagnose_ci_rejects_nonpositive_selected_id(field: str) -> None:
    client = _minimal_ci_client()
    kwargs = {field: 0}
    with pytest.raises(ValidationError):
        diagnose_ci(client, "owner/repo", 10, **kwargs)


def test_diagnose_ci_revision_mismatch() -> None:
    mock_client = MagicMock(spec=GitHubClient)
    mock_client.get_pr.return_value = {
        "number": 10,
        "head": {"sha": "aaaa1111bbbb2222cccc3333dddd4444eeee5555", "ref": "fix-branch"},
    }
    with pytest.raises(RevisionMismatchError):
        diagnose_ci(
            mock_client,
            "owner/repo",
            10,
            expected_head="ffff0000ffff0000ffff0000ffff0000ffff0000",
        )


def test_single_excerpt_always_includes_failure_anchor():
    excerpts, categories = extract_failure_excerpts('context one\ncontext two\nError: broken\ncontext after\n', max_excerpts=1)
    assert len(excerpts) == 1
    assert 'Error: broken' in excerpts[0]['text']
    assert categories


@pytest.mark.parametrize('kwargs', [{'max_excerpts': 0}, {'context_lines': -1}])
def test_invalid_excerpt_bounds(kwargs):
    with pytest.raises(ValidationError):
        extract_failure_excerpts('Error: failure', **kwargs)


def test_failed_job_collection_has_a_network_budget():
    client = _minimal_ci_client()
    client.get_workflow_runs.return_value = {'workflow_runs': [{'id': 1, 'head_sha': 'a'*40}]}
    client.get_run_jobs.return_value = {'jobs': [
        {'id': i + 1, 'conclusion': 'failure'} for i in range(25)]}
    client.get_job_logs.return_value = 'Error: failure\n'
    result = diagnose_ci(client, 'owner/repo', 1)
    assert len(result['failed_jobs']) == 20
    assert result['jobs_limited'] and not result['evidence_complete']
    assert client.get_job_logs.call_count == 20
