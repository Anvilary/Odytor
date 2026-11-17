"""Unified PR evidence retrieval with strict completeness tracking and snapshot consistency."""

from __future__ import annotations

from typing import Any

from git_plus.diagnostics import safe_payload, safe_text
from git_plus.errors import APIError, RevisionMismatchError, ValidationError
from git_plus.github import GitHubClient, validate_repo, validate_sha

_COMMON_INSTRUCTION_PATHS = (
    ".github/copilot-instructions.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
)

_REVIEW_THREADS_GRAPHQL = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 10) {
            nodes {
              id
              author {
                login
              }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


def get_pr_evidence(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    expected_head: str | None = None,
    include_diff: bool = True,
    max_diff_lines: int = 500,
    include_files: bool = True,
    include_reviews: bool = True,
    include_checks: bool = True,
    include_instructions: bool = True,
    files_page: int = 1,
    files_per_page: int = 100,
) -> dict[str, Any]:
    """Retrieve comprehensive, snapshot-consistent PR evidence."""
    owner, name = validate_repo(repo)
    if number < 1:
        raise ValidationError(f"Pull request number must be positive (got {number}).")

    if expected_head:
        expected_head = validate_sha(expected_head, "expected_head")

    initial_pr = client.get_pr(repo, number)
    initial_head = initial_pr.get("head", {}).get("sha", "")
    base_sha = initial_pr.get("base", {}).get("sha", "")

    if expected_head and initial_head.casefold() != expected_head.casefold():
        raise RevisionMismatchError(expected=expected_head, actual=initial_head)

    result: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "head_sha": initial_head,
        "base_sha": base_sha,
        "head_ref": initial_pr.get("head", {}).get("ref"),
        "base_ref": initial_pr.get("base", {}).get("ref"),
        "state": initial_pr.get("state"),
        "is_draft": initial_pr.get("draft", False),
        "title": initial_pr.get("title"),
        "author": initial_pr.get("user", {}).get("login"),
        "created_at": initial_pr.get("created_at"),
        "updated_at": initial_pr.get("updated_at"),
        "html_url": initial_pr.get("html_url"),
        "mergeable": initial_pr.get("mergeable"),
        "mergeable_state": initial_pr.get("mergeable_state"),
        "merged": initial_pr.get("merged", False),
        "body": initial_pr.get("body"),
        "total_commits": initial_pr.get("commits", 0),
        "total_additions": initial_pr.get("additions", 0),
        "total_deletions": initial_pr.get("deletions", 0),
        "total_changed_files": initial_pr.get("changed_files", 0),
        "metadata_complete": True,
    }

    files_complete = True
    if include_files:
        files_data = client.get_pr_files(
            repo,
            number,
            per_page=files_per_page,
            page=files_page,
        )
        file_entries = []
        for f in files_data:
            file_entries.append(
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "changes": f.get("changes"),
                    "patch_excerpt": f.get("patch"),
                }
            )
        result["files"] = file_entries
        total_files = initial_pr.get("changed_files", len(file_entries))
        files_complete = len(file_entries) >= total_files or len(file_entries) == 0
        result["files_complete"] = files_complete
        result["files_page"] = files_page
    else:
        result["files_complete"] = False

    diff_complete = True
    if include_diff:
        raw_diff = client.get_pr_diff(repo, number)
        diff_lines = raw_diff.splitlines()
        total_diff_lines = len(diff_lines)
        if total_diff_lines > max_diff_lines:
            selected_lines = diff_lines[:max_diff_lines]
            diff_complete = False
            result["diff"] = "\n".join(selected_lines)
            result["diff_truncated"] = True
        else:
            result["diff"] = raw_diff
            result["diff_truncated"] = False
        result["diff_total_lines"] = total_diff_lines
        result["diff_returned_lines"] = min(total_diff_lines, max_diff_lines)
        result["diff_complete"] = diff_complete
    else:
        result["diff_complete"] = False

    reviews_complete = True
    if include_reviews:
        try:
            reviews_list = client.get_pr_reviews(repo, number)
            result["reviews"] = [
                {
                    "id": r.get("id"),
                    "user": r.get("user", {}).get("login"),
                    "state": r.get("state"),
                    "submitted_at": r.get("submitted_at"),
                    "body": r.get("body"),
                }
                for r in reviews_list
            ]
        except APIError:
            result["reviews"] = []
            reviews_complete = False

        try:
            gql_data = client.graphql(
                _REVIEW_THREADS_GRAPHQL,
                {"owner": owner, "repo": name, "number": number},
            )
            pr_data = gql_data.get("repository", {}).get("pullRequest", {})
            threads_data = pr_data.get("reviewThreads", {})
            nodes = threads_data.get("nodes", [])
            has_next = threads_data.get("pageInfo", {}).get("hasNextPage", False)
            if has_next:
                reviews_complete = False
            result["review_threads"] = [
                {
                    "id": node.get("id"),
                    "is_resolved": node.get("isResolved"),
                    "is_outdated": node.get("isOutdated"),
                    "path": node.get("path"),
                    "line": node.get("line"),
                    "comments": [
                        {
                            "id": c.get("id"),
                            "author": c.get("author", {}).get("login"),
                            "body": c.get("body"),
                            "created_at": c.get("createdAt"),
                        }
                        for c in node.get("comments", {}).get("nodes", [])
                    ],
                }
                for node in nodes
            ]
        except APIError:
            result["review_threads"] = []
        result["reviews_complete"] = reviews_complete
    else:
        result["reviews_complete"] = False

    checks_complete = True
    if include_checks:
        try:
            check_runs_data = client.get_check_runs(repo, initial_head)
            runs = check_runs_data.get("check_runs", [])
            result["check_runs"] = [
                {
                    "id": run.get("id"),
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "started_at": run.get("started_at"),
                    "completed_at": run.get("completed_at"),
                    "html_url": run.get("html_url"),
                }
                for run in runs
            ]
        except APIError:
            result["check_runs"] = []
            checks_complete = False

        try:
            status_data = client.get_commit_status(repo, initial_head)
            result["combined_status"] = {
                "state": status_data.get("state"),
                "total_count": status_data.get("total_count", 0),
                "statuses": [
                    {
                        "context": s.get("context"),
                        "state": s.get("state"),
                        "description": s.get("description"),
                        "target_url": s.get("target_url"),
                    }
                    for s in status_data.get("statuses", [])
                ],
            }
        except APIError:
            result["combined_status"] = None
        result["checks_complete"] = checks_complete
    else:
        result["checks_complete"] = False

    if include_instructions:
        instructions: dict[str, str] = {}
        for path in _COMMON_INSTRUCTION_PATHS:
            content = client.get_file_content(repo, path, ref=initial_head)
            if content:
                instructions[path] = safe_text(content, 4000)
                break
        result["instructions"] = instructions

    final_pr = client.get_pr(repo, number)
    final_head = final_pr.get("head", {}).get("sha", "")
    if final_head != initial_head:
        result["inconsistent_snapshot"] = True
        result["evidence_complete"] = False
        result["snapshot_drift_warning"] = (
            f"PR head moved during retrieval from {initial_head} to {final_head}."
        )
    else:
        result["inconsistent_snapshot"] = False
        all_complete = (
            files_complete
            and diff_complete
            and reviews_complete
            and checks_complete
        )
        result["evidence_complete"] = all_complete

    return safe_payload(result)
