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
from git_plus.errors import RevisionMismatchError, ValidationError
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
            {"id": 1001, "name": "CI", "conclusion": "failure"}
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

    res = diagnose_ci(mock_client, "owner/repo", 10)
    assert res["number"] == 10
    assert res["total_failed_jobs"] == 1
    assert "test_failure" in res["failure_categories"]
    assert res["evidence_complete"] is True
    assert len(res["failed_jobs"][0]["annotations"]) == 1
    assert res["failed_jobs"][0]["annotations"][0]["path"] == "tests/test_foo.py"


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
