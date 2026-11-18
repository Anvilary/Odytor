"""Run-to-PR association for CI reruns, driven through GitHubClient over a fake GitHub HTTP session.

The default payload is the shape production GitHub returned for alteixeira20/Anvilary-Website PR #1 / run
44350223757: the run's pull_requests[].base.repo carries a numeric `id` and a `name` but no `full_name`.
"""
from __future__ import annotations

import copy
import json
import re
from unittest.mock import MagicMock

import pytest
import requests

from odytor.ci_control import control_ci
from odytor.errors import RevisionMismatchError, SecurityError, ValidationError
from odytor.github import GitHubClient

REPO, NUMBER, RUN, JOB = "alteixeira20/Anvilary-Website", 1, 44350223757, 99
REPO_ID = 1297682797
HEAD, OTHER = "d" * 40, "e" * 40

PR = {"state": "open", "merged": False, "maintainer_can_modify": False,
      "head": {"sha": HEAD, "ref": "test/git-plus-mcp-e2e-20261118", "repo": {"id": REPO_ID, "full_name": REPO, "fork": False}},
      "base": {"sha": "c" * 40, "ref": "main", "repo": {"id": REPO_ID, "full_name": REPO, "name": "Anvilary-Website"}}}
RUN_PAYLOAD = {"id": RUN, "head_sha": HEAD, "event": "pull_request", "status": "completed", "conclusion": "failure", "run_attempt": 1,
               "head_repository": {"id": REPO_ID, "full_name": REPO},
               "pull_requests": [{"number": NUMBER, "base": {"ref": "main", "sha": "c" * 40, "repo": {"id": REPO_ID, "name": "Anvilary-Website"}}}]}
JOB_PAYLOAD = {"id": JOB, "run_id": RUN, "head_sha": HEAD, "status": "completed", "conclusion": "failure"}


class FakeGitHub:
    """A requests-style session that answers like api.github.com and records every mutation."""

    def __init__(self, run=None, pr=None):
        self.run, self.pr = copy.deepcopy(run or RUN_PAYLOAD), copy.deepcopy(pr or PR)
        self.posts: list[str] = []

    def request(self, method, url, **_):
        path = url.split("api.github.com/", 1)[1]
        if method == "POST":
            self.posts.append(path)
            return self._response(201, {})
        routes = {rf"repos/{REPO}/pulls/{NUMBER}": self.pr, rf"repos/{REPO}/actions/runs/{RUN}": self.run,
                  rf"repos/{REPO}/actions/jobs/{JOB}": JOB_PAYLOAD}
        for pattern, body in routes.items():
            if re.fullmatch(pattern, path):
                return self._response(200, body)
        return self._response(404, {"message": "Not Found"})

    @staticmethod
    def _response(status, body):
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        resp._content_consumed = True
        resp.headers["content-type"] = "application/json"
        return resp


def rerun(operation, *, run=None, pr=None, head=HEAD, number=NUMBER, repo=REPO):
    fake = FakeGitHub(run, pr)
    client = GitHubClient(token="ghp_" + "t" * 36, session=fake)
    extra = {"job_id": JOB} if operation == "rerun_job" else {}
    return fake, lambda: control_ci(client, repo, number, head, operation, run_id=RUN, **extra)


def association(*entries):
    run = copy.deepcopy(RUN_PAYLOAD)
    run["pull_requests"] = list(entries)
    return run


def link(number=NUMBER, repo=None):
    return {"number": number, "base": {"ref": "main", "repo": repo} if repo is not None else {"ref": "main"}}


OPERATIONS = ["rerun_failed", "rerun_all", "rerun_job"]
ENDPOINTS = {"rerun_failed": f"repos/{REPO}/actions/runs/{RUN}/rerun-failed-jobs",
             "rerun_all": f"repos/{REPO}/actions/runs/{RUN}/rerun", "rerun_job": f"repos/{REPO}/actions/jobs/{JOB}/rerun"}


@pytest.mark.parametrize("operation", OPERATIONS)
def test_production_payload_with_repository_id_and_no_full_name_reaches_the_mutation(operation):
    fake, call = rerun(operation)
    result = call()
    assert result["accepted"] is True and result["completed"] is False
    assert fake.posts == [ENDPOINTS[operation]]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("identity", [
    {"full_name": REPO},                                   # complete owner/name only
    {"full_name": REPO.upper()},                           # names compare case-insensitively
    {"id": REPO_ID, "full_name": REPO},                    # both, consistent
    {"id": REPO_ID, "name": "Anvilary-Website"},           # id plus the short name GitHub sends
])
def test_every_verified_identity_representation_is_accepted(operation, identity):
    fake, call = rerun(operation, run=association(link(repo=identity)))
    assert call()["accepted"] is True and fake.posts == [ENDPOINTS[operation]]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("repo_obj,why", [
    ({"id": REPO_ID + 1}, "different repository id"),
    ({"id": REPO_ID + 1, "full_name": "someone/else"}, "same PR number in a different repository"),
    ({"full_name": "someone/Anvilary-Website"}, "different owner, same repository name"),
    ({"id": REPO_ID, "full_name": "someone/else"}, "id matches but name contradicts"),
    ({"id": REPO_ID + 1, "full_name": REPO}, "name matches but id contradicts"),
    ({"id": str(REPO_ID)}, "id of the wrong type"),
    ({"name": "Anvilary-Website"}, "short name alone is not an identity"),
    ({}, "no identity at all"),
    (None, "no repository object"),
    ("owner/repo", "repository is not an object"),
])
def test_wrong_missing_or_contradictory_repository_identity_never_reaches_the_mutation(operation, repo_obj, why):
    entry = link(repo=repo_obj) if repo_obj is not None else link()
    fake, call = rerun(operation, run=association(entry))
    with pytest.raises(SecurityError):
        call()
    assert fake.posts == [], why


@pytest.mark.parametrize("operation", OPERATIONS)
def test_wrong_pr_number_is_refused(operation):
    fake, call = rerun(operation, run=association(link(number=2, repo={"id": REPO_ID})))
    with pytest.raises(SecurityError, match="different pull request"):
        call()
    assert fake.posts == []


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("linked", [[], None])
def test_missing_association_is_refused_for_a_same_repository_pr(operation, linked):
    run = copy.deepcopy(RUN_PAYLOAD)
    run["pull_requests"] = linked
    fake, call = rerun(operation, run=run)
    with pytest.raises(SecurityError, match="no pull request association"):
        call()
    assert fake.posts == []


def test_a_fork_pr_run_may_omit_the_association_but_keeps_every_other_guard():
    pr = copy.deepcopy(PR)
    pr["head"]["repo"] = {"id": 5, "full_name": "contributor/Anvilary-Website", "fork": True}
    run = copy.deepcopy(RUN_PAYLOAD)
    run.update(pull_requests=[], head_repository={"id": 5, "full_name": "contributor/Anvilary-Website"})
    fake, call = rerun("rerun_failed", run=run, pr=pr)
    assert call()["accepted"] is True and fake.posts == [ENDPOINTS["rerun_failed"]]
    run["event"] = "push"
    fake, call = rerun("rerun_failed", run=run, pr=pr)
    with pytest.raises(SecurityError, match="pull_request event"):
        call()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_several_associated_pull_requests_are_ambiguous_even_when_one_matches(operation):
    good, other = link(repo={"id": REPO_ID}), link(number=7, repo={"id": REPO_ID})
    for linked in ((good, other), (other, good), (good, good)):
        fake, call = rerun(operation, run=association(*linked))
        with pytest.raises(SecurityError, match="ambiguous"):
            call()
        assert fake.posts == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_stale_expected_head_is_rejected_before_any_run_is_read(operation):
    fake, call = rerun(operation, head=OTHER)
    with pytest.raises(RevisionMismatchError):
        call()
    assert fake.posts == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_run_for_another_revision_or_another_run_is_refused(operation):
    run = copy.deepcopy(RUN_PAYLOAD)
    run["head_sha"] = OTHER
    fake, call = rerun(operation, run=run)
    with pytest.raises(RevisionMismatchError):
        call()
    run = copy.deepcopy(RUN_PAYLOAD)
    run["id"] = RUN + 1
    fake2, call2 = rerun(operation, run=run)
    with pytest.raises(ValidationError, match="different Actions run"):
        call2()
    assert fake.posts == fake2.posts == []


def test_run_from_another_repository_head_is_refused():
    run = copy.deepcopy(RUN_PAYLOAD)
    run["head_repository"] = {"id": 9, "full_name": "someone/else"}
    fake, call = rerun("rerun_all", run=run)
    with pytest.raises(SecurityError, match="head repository"):
        call()
    assert fake.posts == []


def test_trusted_repository_identity_comes_from_the_pr_not_from_the_run_alone():
    """A run claiming an id is compared with the id GitHub reports for the requested repository."""
    pr = copy.deepcopy(PR)
    pr["base"]["repo"]["id"] = 424242
    fake, call = rerun("rerun_all", pr=pr)  # run says REPO_ID, the PR's base repository says 424242
    with pytest.raises(SecurityError, match="different repository"):
        call()
    assert fake.posts == []
    pr["base"]["repo"].pop("id")  # without a trusted id, an id-only link is unverifiable
    fake, call = rerun("rerun_all", pr=pr)
    with pytest.raises(SecurityError, match="verifiable repository identity"):
        call()
    assert fake.posts == []
