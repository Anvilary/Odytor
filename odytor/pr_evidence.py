"""Unified PR evidence retrieval with strict completeness tracking and snapshot consistency."""

from __future__ import annotations

from typing import Any

from odytor.diagnostics import safe_payload, safe_text, safe_text_tracked
from odytor.errors import APIError, RevisionMismatchError, ValidationError
from odytor.github import GitHubClient, validate_repo, validate_sha

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
            pageInfo { hasNextPage }
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


GITHUB_FILE_LIST_CAP = 3000        # the files endpoint lists at most this many files
ASSEMBLED_DIFF_MAX_BYTES = 8 * 1024 * 1024
AUTO_PATCH_FILE_LIMIT = 30         # per-file patches are included by default only for PRs up to this many files
NO_PATCH_NOTE = "# odytor: patch not provided by GitHub for this file (binary, or too large)"


def _diff_from_files(client: GitHubClient, repo: str, number: int) -> tuple[str, dict[str, Any]]:
    """Rebuild a unified diff from the paginated files API when GitHub refuses the whole diff.

    GitHub omits `patch` for binary and oversized files; those are listed, never silently skipped.
    """
    parts: list[str] = []
    missing: list[str] = []
    listed = size = 0
    capped = False
    for page in range(1, GITHUB_FILE_LIST_CAP // 100 + 1):
        batch = client.get_pr_files(repo, number, per_page=100, page=page)
        for entry in batch:
            name = str(entry.get("filename") or "")
            old = str(entry.get("previous_filename") or name)
            status = entry.get("status")
            patch = entry.get("patch")
            head = [f"diff --git a/{old} b/{name}",
                    "--- /dev/null" if status == "added" else f"--- a/{old}",
                    "+++ /dev/null" if status == "removed" else f"+++ b/{name}"]
            if isinstance(patch, str) and patch:
                chunk = "\n".join([*head, patch])
            else:
                missing.append(name)
                chunk = "\n".join([*head, NO_PATCH_NOTE])
            listed += 1
            size += len(chunk.encode("utf-8", errors="replace")) + 1
            if size > ASSEMBLED_DIFF_MAX_BYTES:
                capped = True
                break
            parts.append(chunk)
        if capped or len(batch) < 100:
            break
    return "\n".join(parts), {"files_listed": listed, "files_without_patch": missing, "byte_capped": capped}


def get_pr_evidence(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    expected_head: str | None = None,
    include_diff: bool = True,
    max_diff_lines: int = 500,
    max_diff_bytes: int = 1048576,
    include_files: bool = True,
    include_reviews: bool = True,
    include_checks: bool = True,
    include_instructions: bool = True,
    files_page: int = 1,
    files_per_page: int = 100,
    diff_offset: int = 0,
    include_file_patches: bool | None = None,
) -> dict[str, Any]:
    """Retrieve comprehensive, snapshot-consistent PR evidence.

    Very large PRs are read in windows: `files_page` pages the file list and `diff_offset` pages the diff (the next
    window starts at diff_offset + diff_returned_lines). Per-file patches are included by default only for PRs of at
    most AUTO_PATCH_FILE_LIMIT files, because the diff already carries them.
    """
    owner, name = validate_repo(repo)
    if number < 1:
        raise ValidationError(f"Pull request number must be positive (got {number}).")
    if not 1 <= files_page or not 1 <= files_per_page <= 100:
        raise ValidationError("files_page must be positive and files_per_page must be between 1 and 100.")
    if not 1 <= max_diff_bytes <= 8 * 1024 * 1024:
        raise ValidationError("max_diff_bytes must be positive and at most 8 MiB.")
    if not 1 <= max_diff_lines <= 5000:
        raise ValidationError("max_diff_lines must be positive.")
    if type(diff_offset) is not int or diff_offset < 0:
        raise ValidationError("diff_offset must be zero or a positive line offset.")

    if expected_head:
        expected_head = validate_sha(expected_head, "expected_head")

    initial_pr = client.get_pr(repo, number)
    initial_head = initial_pr.get("head", {}).get("sha", "")
    base_sha = initial_pr.get("base", {}).get("sha", "")
    base_ref = initial_pr.get("base", {}).get("ref")

    if expected_head and initial_head.casefold() != expected_head.casefold():
        raise RevisionMismatchError(expected=expected_head, actual=initial_head)

    live_target_tip = None
    if base_ref and hasattr(client, "request"):
        try:
            resp = client.request("GET", f"repos/{repo}/git/ref/heads/{base_ref}")
            data = resp.json() if hasattr(resp, "json") else {}
            if isinstance(data, dict) and "object" in data:
                live_target_tip = data["object"].get("sha")
        except Exception:
            live_target_tip = None

    result: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "head_sha": initial_head,
        "base_sha": base_sha,
        "pr_api_base_snapshot": base_sha,
        "live_target_branch_tip": live_target_tip,
        "base_snapshot_is_current": (base_sha == live_target_tip) if (base_sha and live_target_tip) else False,
        "head_ref": initial_pr.get("head", {}).get("ref"),
        "base_ref": base_ref,
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
    total_files = initial_pr.get("changed_files", 0)
    patches_wanted = include_file_patches if include_file_patches is not None else total_files <= AUTO_PATCH_FILE_LIMIT
    if include_files:
        files_data = client.get_pr_files(
            repo,
            number,
            per_page=files_per_page,
            page=files_page,
        )
        file_entries = []
        for f in files_data:
            entry = {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "changes": f.get("changes"),
            }
            if f.get("previous_filename"):
                entry["previous_filename"] = f.get("previous_filename")
            if patches_wanted:
                entry["patch_excerpt"] = f.get("patch")
            file_entries.append(entry)
        result["files"] = file_entries
        total_files = initial_pr.get("changed_files", len(file_entries))
        files_complete = files_page == 1 and len(file_entries) >= total_files
        result["files_complete"] = files_complete
        result["files_page"] = files_page
        result["files_total"] = total_files
        result["files_next_page"] = files_page + 1 if files_page * files_per_page < total_files else None
        result["file_patches_included"] = bool(patches_wanted)
    else:
        result["files_complete"] = False

    diff_complete = True
    if include_diff:
        diff_source, fallback = "pull_diff", None
        try:
            raw_diff = client.get_pr_diff(repo, number)
        except APIError as exc:
            if not client.is_diff_too_large(exc):
                raise
            raw_diff, fallback = _diff_from_files(client, repo, number)
            diff_source = "files_api"
            result["diff_fallback_reason"] = safe_text(str(exc.diagnostic.get("message", "")), 300)
        all_lines = raw_diff.splitlines()
        window = all_lines[diff_offset:diff_offset + max_diff_lines]
        window_text = "\n".join(window)
        bounded_diff, byte_cut = safe_text_tracked(window_text, limit=None, max_bytes=max_diff_bytes)
        redacted = bounded_diff != window_text and not byte_cut
        has_more = diff_offset + len(window) < len(all_lines)
        result["diff"] = bounded_diff
        result["diff_source"] = diff_source
        result["diff_offset"] = diff_offset
        result["diff_has_more"] = has_more
        result["diff_truncated"] = has_more or byte_cut or diff_offset > 0
        result["diff_redacted"] = redacted
        diff_complete = diff_offset == 0 and not has_more and not byte_cut and not redacted
        diff_file_count = sum(line.startswith("diff --git ") for line in all_lines)
        if fallback is not None:
            result["diff_files_listed"] = fallback["files_listed"]
            result["diff_files_total"] = total_files
            result["diff_files_without_patch"] = fallback["files_without_patch"][:50]
            if fallback["files_without_patch"] or fallback["byte_capped"] or fallback["files_listed"] != total_files:
                diff_complete = False
        elif diff_file_count != initial_pr.get("changed_files", diff_file_count):
            diff_complete = False
        result["diff_total_lines"] = len(all_lines)
        result["diff_returned_lines"] = len(window)
        result["diff_complete"] = diff_complete
    else:
        result["diff_complete"] = False

    reviews_complete = True
    if include_reviews:
        try:
            reviews_list = client.get_pr_reviews(repo, number)
            if len(reviews_list) >= 100:
                reviews_complete = False
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
            if any(n.get("comments", {}).get("pageInfo", {}).get("hasNextPage") for n in nodes):
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
            reviews_complete = False
        result["reviews_complete"] = reviews_complete
    else:
        result["reviews_complete"] = False

    checks_complete = True
    if include_checks:
        try:
            check_runs_data = client.get_check_runs(repo, initial_head)
            runs = check_runs_data.get("check_runs", [])
            if len(runs) >= 100 or check_runs_data.get("total_count", len(runs)) > len(runs):
                checks_complete = False
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
            checks_complete = False
        else:
            if status_data.get("total_count", len(status_data.get("statuses", []))) > len(status_data.get("statuses", [])):
                checks_complete = False

        runs_list = result.get("check_runs", [])
        statuses_list = (result.get("combined_status") or {}).get("statuses", [])
        passed_runs = sum(1 for r in runs_list if r.get("conclusion") in ("success", "neutral"))
        failed_runs = sum(1 for r in runs_list if r.get("conclusion") in ("failure", "timed_out", "action_required"))
        in_progress_runs = sum(1 for r in runs_list if r.get("status") in ("in_progress", "queued"))
        skipped_runs = sum(1 for r in runs_list if r.get("conclusion") == "skipped")
        result["checks_summary"] = {
            "head_sha": initial_head,
            "total_check_runs": len(runs_list),
            "passed": passed_runs,
            "failed": failed_runs,
            "in_progress": in_progress_runs,
            "skipped": skipped_runs,
            "combined_state": (result.get("combined_status") or {}).get("state"),
            "scope": "github_ci_head",
        }
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
    final_base = final_pr.get("base", {}).get("sha", "")
    if final_head != initial_head or final_base != base_sha:
        result["inconsistent_snapshot"] = True
        result["evidence_complete"] = False
        result["snapshot_drift_warning"] = (
            f"PR head moved or base changed during retrieval (head {initial_head} to {final_head}, base {base_sha} to {final_base})."
        )
    else:
        result["inconsistent_snapshot"] = False
        all_complete = (
            files_complete
            and diff_complete
            and reviews_complete
            and checks_complete
            and include_files
            and include_diff
            and include_reviews
            and include_checks
            and include_instructions
        )
        result["evidence_complete"] = all_complete

    return safe_payload(result)
