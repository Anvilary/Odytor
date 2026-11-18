"""pr_evidence on very large PRs, through GitHubClient over a fake GitHub HTTP session.

GitHub's diff endpoint refuses PRs over 20,000 lines or 300 files with HTTP 406 (observed on real PRs: odysseus
#7283, #7292, #7311); the files endpoint (100 per page, at most 3,000 files) keeps working.
"""
from __future__ import annotations

import json
import re

import pytest
import requests

from git_plus.diagnostics import safe_payload
from git_plus.github import GitHubClient
from git_plus.pr_evidence import get_pr_evidence

REPO, NUMBER, HEAD, BASE = "owner/big", 9, "a" * 40, "b" * 40
TOO_MANY_LINES = {"message": "Sorry, the diff exceeded the maximum number of lines (20000)",
                  "errors": [{"resource": "PullRequest", "field": "diff", "code": "too_large"}]}
TOO_MANY_FILES = {"message": "Sorry, the diff exceeded the maximum number of files (300). Consider using "
                             "'List pull requests files' API or locally cloning the repository instead."}


def make_files(count, *, lines_per_file=120, omit_patch=()):
    files = []
    for i in range(count):
        body = "\n".join(f"+line {j} of file {i}" for j in range(lines_per_file))
        entry = {"filename": f"src/module_{i:04d}.py", "status": "added", "additions": lines_per_file, "deletions": 0,
                 "changes": lines_per_file, "patch": f"@@ -0,0 +1,{lines_per_file} @@\n{body}"}
        if i in omit_patch:
            del entry["patch"]
        files.append(entry)
    return files


class FakeGitHub:
    def __init__(self, files, *, diff_status=406, diff_body=TOO_MANY_LINES, diff_text=None, calls=None):
        self.files, self.diff_status, self.diff_body, self.diff_text = files, diff_status, diff_body, diff_text
        self.requests = calls if calls is not None else []
        self.pr = {"number": NUMBER, "state": "open", "title": "huge", "user": {"login": "dev"}, "changed_files": len(files),
                   "additions": sum(f["additions"] for f in files), "deletions": 0, "commits": 40,
                   "head": {"sha": HEAD, "ref": "big", "repo": {"full_name": REPO}},
                   "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}}, "body": "x"}

    def request(self, method, url, headers=None, params=None, **kwargs):
        path = url.split("api.github.com/", 1)[1]
        self.requests.append((path, dict(params or {}), kwargs.get("timeout")))
        if re.fullmatch(rf"repos/{REPO}/pulls/{NUMBER}", path):
            if "diff" in (headers or {}).get("Accept", ""):
                return self._response(self.diff_status, self.diff_body if self.diff_text is None else self.diff_text)
            return self._response(200, self.pr)
        if path == f"repos/{REPO}/pulls/{NUMBER}/files":
            page, per = params["page"], params["per_page"]
            return self._response(200, self.files[(page - 1) * per:page * per])
        if path.startswith("graphql"):
            return self._response(200, {"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}})
        if "/reviews" in path or "/check-runs" in path or "/status" in path or "/contents/" in path:
            return self._response(200 if "/check-runs" not in path else 200, {"check_runs": [], "state": "success", "statuses": [], "total_count": 0} if "/check-runs" in path or "/status" in path else [])
        return self._response(404, {"message": "Not Found"})

    @staticmethod
    def _response(status, body):
        resp = requests.Response()
        resp.status_code = status
        resp._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        resp._content_consumed = True
        resp.headers["content-type"] = "application/json"
        return resp


def evidence(files, **kwargs):
    fake = FakeGitHub(files, **{k: kwargs.pop(k) for k in list(kwargs) if k in {"diff_status", "diff_body", "diff_text"}})
    client = GitHubClient(token="ghp_" + "t" * 36, session=fake)
    return get_pr_evidence(client, REPO, NUMBER, expected_head=HEAD, **kwargs), fake


@pytest.mark.parametrize("body,count", [(TOO_MANY_LINES, 250), (TOO_MANY_FILES, 330)])
def test_diff_refused_by_github_falls_back_to_the_files_api(body, count):
    result, fake = evidence(make_files(count), diff_body=body, max_diff_lines=5000)
    assert result["diff_source"] == "files_api" and "exceeded" in result["diff_fallback_reason"]
    assert result["diff"].startswith("diff --git a/src/module_0000.py b/src/module_0000.py")
    assert result["diff_total_lines"] > 20000 or count == 330
    assert result["diff"].count("diff --git ") >= 1 and result["diff_complete"] is False  # a page, not the whole diff
    pages = [params["page"] for path, params, _ in fake.requests if path.endswith("/files") and params["per_page"] == 100]
    assert pages and max(pages) == -(-count // 100)  # every page of the file list was read


def test_assembled_diff_is_complete_when_every_patch_is_present_and_the_window_covers_it():
    result, _ = evidence(make_files(3, lines_per_file=5), diff_body=TOO_MANY_LINES, include_files=False)
    assert result["diff_source"] == "files_api" and result["diff_complete"] is True and result["diff_truncated"] is False
    assert result["diff"].count("diff --git ") == 3 and "+++ b/src/module_0002.py" in result["diff"]


def test_files_without_a_patch_make_the_assembled_diff_incomplete_and_are_named():
    result, _ = evidence(make_files(4, lines_per_file=5, omit_patch={1, 3}), diff_body=TOO_MANY_LINES, include_files=False)
    assert result["diff_complete"] is False and result["diff_files_without_patch"] == ["src/module_0001.py", "src/module_0003.py"]
    assert "patch not provided by GitHub" in result["diff"]


def test_github_file_cap_is_reported_not_hidden():
    files = make_files(3000, lines_per_file=1)
    fake = FakeGitHub(files)
    fake.pr["changed_files"] = 3400  # GitHub lists at most 3,000 files
    client = GitHubClient(token="ghp_" + "t" * 36, session=fake)
    result = get_pr_evidence(client, REPO, NUMBER, expected_head=HEAD, include_files=False, max_diff_lines=50)
    assert result["diff_complete"] is False and result["diff_files_listed"] == 3000 and result["diff_files_total"] == 3400


def test_other_diff_errors_are_not_swallowed():
    from git_plus.errors import APIError
    with pytest.raises(APIError):
        evidence(make_files(2), diff_status=500, diff_body={"message": "boom"}, include_files=False)


def test_diff_can_be_read_in_windows_beyond_the_first_page():
    text = "\n".join(f"diff line {i}" for i in range(12000))
    first, _ = evidence(make_files(1), diff_status=200, diff_text=text, include_files=False, max_diff_lines=5000)
    second, _ = evidence(make_files(1), diff_status=200, diff_text=text, include_files=False, max_diff_lines=5000, diff_offset=5000)
    last, _ = evidence(make_files(1), diff_status=200, diff_text=text, include_files=False, max_diff_lines=5000, diff_offset=10000)
    assert first["diff"].startswith("diff line 0\n") and first["diff_has_more"] is True and first["diff_complete"] is False
    assert second["diff"].startswith("diff line 5000\n") and second["diff_offset"] == 5000 and second["diff_returned_lines"] == 5000
    assert last["diff"].endswith("diff line 11999") and last["diff_returned_lines"] == 2000 and last["diff_has_more"] is False
    assert last["diff_complete"] is False  # a later window is never the whole diff


def test_a_large_pr_lists_all_files_in_pages_without_patches_within_the_response_budget():
    files = make_files(330)
    pages = []
    for page in (1, 2, 3, 4):
        result, _ = evidence(files, include_diff=False, include_file_patches=False, files_page=page, files_per_page=100)
        payload = safe_payload(result)
        assert result["file_patches_included"] is False and all("patch_excerpt" not in f for f in result["files"])
        assert len(json.dumps(payload)) <= 65536 and "truncated_fields" not in payload
        assert result["files_total"] == 330
        pages.extend(f["filename"] for f in result["files"])
        assert result["files_next_page"] == (page + 1 if page < 4 else None)
    assert pages == [f["filename"] for f in files]


def test_small_prs_keep_their_per_file_patches_and_large_ones_drop_them_by_default():
    small, _ = evidence(make_files(5, lines_per_file=3), diff_status=200, diff_text="d\n", include_diff=True)
    assert small["file_patches_included"] is True and "patch_excerpt" in small["files"][0]
    big, _ = evidence(make_files(60, lines_per_file=3), diff_status=200, diff_text="d\n")
    assert big["file_patches_included"] is False and "patch_excerpt" not in big["files"][0]


def test_requests_use_a_generous_read_timeout():
    _, fake = evidence(make_files(2), diff_body=TOO_MANY_LINES)
    assert all(timeout == (10.0, 120.0) for _, _, timeout in fake.requests)
