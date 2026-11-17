"""Tests for pr_evidence tool."""

from unittest.mock import MagicMock
import pytest
from git_plus.errors import APIError, RevisionMismatchError, ValidationError
from git_plus.github import GitHubClient
from git_plus.pr_evidence import get_pr_evidence


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=GitHubClient)
    client.get_pr.return_value = {
        "number": 42,
        "state": "open",
        "draft": False,
        "title": "Add feature X",
        "user": {"login": "alice"},
        "head": {"sha": "1111111111111111111111111111111111111111", "ref": "feat-x"},
        "base": {"sha": "0000000000000000000000000000000000000000", "ref": "main"},
        "changed_files": 1,
        "body": "PR description",
    }
    client.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py\n+print('hello')\n"
    client.get_pr_files.return_value = [
        {"filename": "foo.py", "status": "modified", "additions": 1, "deletions": 0}
    ]
    client.get_pr_reviews.return_value = [
        {"id": 1, "user": {"login": "bob"}, "state": "APPROVED", "body": "LGTM"}
    ]
    client.graphql.return_value = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [],
                }
            }
        }
    }
    client.get_check_runs.return_value = {
        "check_runs": [
            {"id": 101, "name": "tests", "status": "completed", "conclusion": "success"}
        ]
    }
    client.get_commit_status.return_value = {"state": "success", "total_count": 0, "statuses": []}
    client.get_file_content.return_value = "Instructions content"
    return client


def test_pr_evidence_full_success(mock_client: MagicMock) -> None:
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["repo"] == "owner/repo"
    assert res["number"] == 42
    assert res["head_sha"] == "1111111111111111111111111111111111111111"
    assert res["evidence_complete"] is True
    assert res["diff_complete"] is True
    assert res["files_complete"] is True
    assert res["inconsistent_snapshot"] is False
    assert len(res["files"]) == 1
    assert len(res["reviews"]) == 1
    assert len(res["check_runs"]) == 1


def test_pr_evidence_expected_head_match(mock_client: MagicMock) -> None:
    res = get_pr_evidence(
        mock_client,
        "owner/repo",
        42,
        expected_head="1111111111111111111111111111111111111111",
    )
    assert res["head_sha"] == "1111111111111111111111111111111111111111"


def test_pr_evidence_expected_head_mismatch(mock_client: MagicMock) -> None:
    with pytest.raises(RevisionMismatchError) as exc_info:
        get_pr_evidence(
            mock_client,
            "owner/repo",
            42,
            expected_head="2222222222222222222222222222222222222222",
        )
    assert exc_info.value.expected == "2222222222222222222222222222222222222222"
    assert exc_info.value.actual == "1111111111111111111111111111111111111111"


def test_pr_evidence_diff_truncated(mock_client: MagicMock) -> None:
    mock_client.get_pr_diff.return_value = "\n".join([f"line {i}" for i in range(100)])
    res = get_pr_evidence(mock_client, "owner/repo", 42, max_diff_lines=10)
    assert res["diff_complete"] is False
    assert res["evidence_complete"] is False
    assert res["diff_truncated"] is True
    assert res["diff_total_lines"] == 100
    assert res["diff_returned_lines"] == 10


def test_pr_evidence_snapshot_drift(mock_client: MagicMock) -> None:
    # First call returns initial head, second call returns moved head
    mock_client.get_pr.side_effect = [
        {
            "number": 42,
            "state": "open",
            "head": {"sha": "1111111111111111111111111111111111111111", "ref": "feat"},
            "base": {"sha": "0000000000000000000000000000000000000000", "ref": "main"},
            "changed_files": 1,
        },
        {
            "number": 42,
            "state": "open",
            "head": {"sha": "3333333333333333333333333333333333333333", "ref": "feat"},
            "base": {"sha": "0000000000000000000000000000000000000000", "ref": "main"},
            "changed_files": 2,
        },
    ]
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["inconsistent_snapshot"] is True
    assert res["evidence_complete"] is False
    assert "PR head moved" in res["snapshot_drift_warning"]


def test_pr_evidence_invalid_repo(mock_client: MagicMock) -> None:
    with pytest.raises(ValidationError):
        get_pr_evidence(mock_client, "invalid-repo", 42)


def test_pr_evidence_invalid_number(mock_client: MagicMock) -> None:
    with pytest.raises(ValidationError):
        get_pr_evidence(mock_client, "owner/repo", -1)


def test_empty_file_page_is_incomplete_when_pr_has_changed_files(mock_client: MagicMock) -> None:
    mock_client.get_pr_files.return_value = []
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["files_complete"] is False
    assert res["evidence_complete"] is False


def test_omitted_sections_are_not_reported_complete(mock_client: MagicMock) -> None:
    res = get_pr_evidence(
        mock_client, "owner/repo", 42,
        include_files=False, include_diff=False, include_reviews=False,
        include_checks=False, include_instructions=False,
    )
    assert res["evidence_complete"] is False


def test_base_drift_marks_snapshot_inconsistent(mock_client: MagicMock) -> None:
    initial = mock_client.get_pr.return_value
    final = {**initial, "base": {"sha": "2222222222222222222222222222222222222222", "ref": "main"}}
    mock_client.get_pr.side_effect = [initial, final]
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["inconsistent_snapshot"] is True
    assert res["evidence_complete"] is False


def test_thread_comment_page_limit_marks_reviews_incomplete(mock_client: MagicMock) -> None:
    mock_client.graphql.return_value["repository"]["pullRequest"]["reviewThreads"] = {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{
            "comments": {"pageInfo": {"hasNextPage": True}, "nodes": []},
        }],
    }
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["reviews_complete"] is False
    assert res["evidence_complete"] is False


def test_graphql_failure_marks_reviews_incomplete(mock_client: MagicMock) -> None:
    mock_client.graphql.side_effect = APIError("GraphQL unavailable")
    res = get_pr_evidence(mock_client, "owner/repo", 42)
    assert res["reviews_complete"] is False
    assert res["evidence_complete"] is False
