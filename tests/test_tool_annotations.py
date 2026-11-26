"""Strict verification of MCP tool annotations across all 31 registered tools."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from fastmcp import Client
import pytest

from odytor.artifacts import ArtifactStore
from odytor.campaign import CampaignRecord, CampaignStore
from odytor.config import ArtifactSettings, RunSettings, Settings, WorkspaceSettings
from odytor.git_workspace import GitWorkspace
from odytor.jobs import JobService, RunStore
from odytor.publish import PlanStore
from odytor.server import create_server
from odytor.workspace import WorkspaceService


READ_ONLY_ALLOWLIST = frozenset({
    "pr_evidence",
    "ci_diagnose",
    "pr_conflicts",
    "pr_rebase_preview",
    "test_run_list",
    "test_result",
    "workspace_read",
    "workspace_search",
    "workspace_diff",
    "workspace_list",
    "workspace_status",
    "campaign_status",
    "artifact_get",
    "odytor_capabilities",
})

DESTRUCTIVE_ALLOWLIST = frozenset({
    "pr_rebase",
    "ci_control",
    "workspace_manage",
    "test_cancel",
    "campaign_cancel",
    "browser_close",
    "pr_body_apply",
})

IDEMPOTENT_ALLOWLIST = READ_ONLY_ALLOWLIST | frozenset({
    "test_cancel",
    "campaign_cancel",
    "browser_close",
    "publish_apply",
    "pr_body_apply",
})

OPEN_WORLD_ALLOWLIST = frozenset({
    "pr_evidence",
    "ci_diagnose",
    "quick_fix",
    "pr_conflicts",
    "pr_rebase",
    "pr_rebase_preview",
    "ci_control",
    "test_run",
    "test_run_list",
    "test_result",
    "workspace_create",
    "workspace_run",
    "test_cancel",
    "browser_run",
    "validation_campaign",
    "campaign_cancel",
    "publish_plan",
    "publish_apply",
    "pr_body_plan",
    "pr_body_apply",
    "odytor_capabilities",
})


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", *args],
        cwd=cwd,
    )
    return out.decode().strip()


def _snapshot_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        # JobService creates/removes its per-process instance lock during start/shutdown, not tool reads.
        if rel.startswith("instances/") or rel.endswith("/lock") or rel == "campaigns/.lock":
            continue
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _settings(tmp_path: Path, *, policies_path: Path | None = None, allow_publish_apply: bool = False) -> Settings:
    return Settings(
        github_token="t",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("owner/repo",),
        workspaces=WorkspaceSettings(
            data_dir=str(tmp_path / "ws"),
            min_free_bytes=64 * 1024 * 1024,
            allow_publish_apply=allow_publish_apply,
        ),
        artifacts=ArtifactSettings(data_dir=str(tmp_path / "artifacts")),
        testing=RunSettings(
            runner_command=("true",),
            policies_path=str(policies_path or (tmp_path / "policies.toml")),
            data_dir=str(tmp_path / "runs"),
            min_free_bytes=64 * 1024 * 1024,
        ),
    )


def test_all_tools_have_strict_boolean_annotations_and_titles(tmp_path) -> None:
    (tmp_path / "req.txt").write_text("pytest==8.3.5\n")
    (tmp_path / "policies.toml").write_text('[repos."owner/repo"]\nrequirements = "req.txt"\ntest_dirs = ["tests"]\n')
    server = create_server(_settings(tmp_path))

    async def inspect():
        async with Client(server) as client:
            return await client.list_tools()

    tools = asyncio.run(inspect())
    try:
        assert len(tools) == 31
        by_name = {t.name: t for t in tools}
        assert len(by_name) == 31

        titles: set[str] = set()
        read_only_tools: set[str] = set()
        destructive_tools: set[str] = set()
        idempotent_tools: set[str] = set()
        open_world_tools: set[str] = set()

        for name, tool in by_name.items():
            assert tool.annotations is not None, f"{name} is missing annotations"
            dumped = tool.annotations.model_dump(by_alias=True)
            title = dumped.get("title")
            assert isinstance(title, str) and title.strip(), f"{name} is missing a human-readable title"
            assert title not in titles, f"Duplicate tool title: {title}"
            titles.add(title)

            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                assert hint in dumped, f"{name} omitted {hint}"
                val = dumped[hint]
                assert type(val) is bool, f"{name}.{hint} must be bool, got {type(val).__name__}: {val!r}"

            if dumped["readOnlyHint"]:
                read_only_tools.add(name)
                assert dumped["destructiveHint"] is False, f"Read-only tool {name} must not set destructiveHint=True"
                assert dumped["idempotentHint"] is True, f"Read-only tool {name} must set idempotentHint=True"
            if dumped["destructiveHint"]:
                destructive_tools.add(name)
            if dumped["idempotentHint"]:
                idempotent_tools.add(name)
            if dumped["openWorldHint"]:
                open_world_tools.add(name)

        assert read_only_tools == READ_ONLY_ALLOWLIST
        assert destructive_tools == DESTRUCTIVE_ALLOWLIST
        assert idempotent_tools == IDEMPOTENT_ALLOWLIST
        assert open_world_tools == OPEN_WORLD_ALLOWLIST
    finally:
        if server.test_service is not None:
            server.test_service.shutdown()


def test_read_only_tools_succeed_when_github_and_storage_fail_on_writes(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "remote_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "--initial-branch=main")
    (repo_dir / "src").mkdir()
    (repo_dir / "src/foo.py").write_text("line 1\nline 2\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "base")
    base_sha = _git(repo_dir, "rev-parse", "HEAD")

    _git(repo_dir, "checkout", "-qb", "feature")
    (repo_dir / "src/foo.py").write_text("line 1\nfeature line 2\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "feature change")
    head_sha = _git(repo_dir, "rev-parse", "HEAD")

    _git(repo_dir, "checkout", "-q", "main")
    (repo_dir / "other.txt").write_text("main advance\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "main advance")
    live_base_sha = _git(repo_dir, "rev-parse", "HEAD")

    (tmp_path / "req.txt").write_text("pytest==8.3.5\n")
    policies_path = tmp_path / "policies.toml"
    policies_path.write_text('[repos."owner/repo"]\nrequirements = "req.txt"\ntest_dirs = ["tests"]\n')

    def fetch_tip(self, target, ref, label, **kwargs):
        _git(self.path, "-c", "protocol.file.allow=always", "fetch", "-q", str(repo_dir), f"{ref}:refs/odytor/{label}")
        return _git(self.path, "rev-parse", f"refs/odytor/{label}")

    def fetch(self, target, ref, expected, label, **kwargs):
        actual = fetch_tip(self, target, ref, label)
        return self.verify_fetched_revision(
            label, expected, actual, allow_base_ancestor=kwargs.get("allow_base_ancestor", False)
        )

    monkeypatch.setattr(GitWorkspace, "fetch_tip", fetch_tip)
    monkeypatch.setattr(GitWorkspace, "fetch", fetch)

    class ReadOnlyGitHubClient:
        def __init__(self) -> None:
            self.token = "t"
            self.base_url = "https://api.github.com"
            self.restricted_repo = None
            self.principal = "local"

        def request(self, method: str, path: str, **kwargs):
            if method.upper() != "GET":
                raise AssertionError(f"GitHub write attempted: {method} {path}")
            if path == "repos/owner/repo":
                return SimpleNamespace(json=lambda: {"private": False, "permissions": {"pull": True}})
            if path == f"repos/owner/repo/git/ref/heads/main":
                return SimpleNamespace(json=lambda: {"object": {"sha": live_base_sha}})
            if path == "repos/owner/repo/code-scanning/alerts":
                return SimpleNamespace(json=lambda: [])
            raise AssertionError(f"Unexpected GET path: {path}")

        def get_pr(self, repo: str, number: int) -> dict:
            return {
                "state": "open",
                "merged": False,
                "draft": False,
                "title": "Sample PR",
                "body": "Body",
                "user": {"login": "alice"},
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/1",
                "mergeable": True,
                "mergeable_state": "clean",
                "commits": 1,
                "additions": 1,
                "deletions": 1,
                "changed_files": 1,
                "head": {"sha": head_sha, "ref": "feature", "repo": {"full_name": "owner/repo"}},
                "base": {"sha": live_base_sha, "ref": "main", "repo": {"full_name": "owner/repo"}},
            }

        def get_pr_files(self, repo: str, number: int, *, per_page: int = 100, page: int = 1) -> list[dict]:
            return [{"filename": "src/foo.py", "status": "modified", "additions": 1, "deletions": 1, "changes": 2, "patch": "@@ -1,2 +1,2 @@\n line 1\n-line 2\n+feature line 2"}]

        def get_pr_diff(self, repo: str, number: int) -> str:
            return "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,2 +1,2 @@\n line 1\n-line 2\n+feature line 2\n"

        def get_pr_reviews(self, repo: str, number: int) -> list[dict]:
            return []

        def graphql(self, query: str, variables: dict | None = None) -> dict:
            if "mutation" in query.lower():
                raise AssertionError("GraphQL mutation attempted during read-only tool call")
            return {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}

        def get_check_runs(self, repo: str, ref: str) -> dict:
            return {"total_count": 0, "check_runs": []}

        def get_commit_status(self, repo: str, ref: str) -> dict:
            return {"state": "success", "total_count": 0, "statuses": []}

        def get_file_content(self, repo: str, path: str, *, ref: str | None = None) -> str | None:
            return None

        def get_workflow_runs(self, repo: str, *, head_sha: str | None = None) -> dict:
            return {
                "total_count": 1,
                "workflow_runs": [{"id": 101, "name": "CI", "head_sha": head_sha, "conclusion": "failure"}],
            }

        def get_run_jobs(self, repo: str, run_id: int) -> dict:
            return {
                "total_count": 1,
                "jobs": [
                    {
                        "id": 202,
                        "name": "pytest",
                        "status": "completed",
                        "conclusion": "failure",
                        "steps": [{"name": "Run tests", "number": 1, "conclusion": "failure"}],
                        "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/303",
                    }
                ],
            }

        def get_job_logs(self, repo: str, job_id: int) -> str:
            return "FAILED tests/test_a.py::test_fail - AssertionError\n"

        def get_check_run_annotations(self, repo: str, check_run_id: int) -> list[dict]:
            return [{"path": "src/foo.py", "start_line": 1, "end_line": 1, "annotation_level": "failure", "title": "Fail", "message": "oops"}]

    gh_client = ReadOnlyGitHubClient()

    @contextlib.contextmanager
    def fake_github_client(cfg, remote=False, *, repo=None):
        yield gh_client

    monkeypatch.setattr("odytor.server.github_client", fake_github_client)
    monkeypatch.setattr("odytor.server.caller_principal", lambda cfg, remote=False: "local")

    server = create_server(_settings(tmp_path, policies_path=policies_path))
    try:
        # Seed durable workspace, run, campaign, and artifact records before enabling write failures.
        ws_tool_create = asyncio.run(server.get_tool("workspace_create"))
        ws_tool_edit = asyncio.run(server.get_tool("workspace_edit"))
        created_ws = ws_tool_create.fn(repo="owner/repo", source="pr_head", number=1, expected_head=head_sha)
        ws_id = created_ws["workspace_id"]
        edited_ws = ws_tool_edit.fn(workspace_id=ws_id, expected_version=0, operation="write", path="notes.txt", content="hello\n")
        assert edited_ws["version"] == 1

        now = time.time()
        run_id = "test-" + "a" * 32
        other_pr_run_id = "test-" + "c" * 32
        server.test_service.start()
        base_record = {
            "schema": 1,
            "owner": "local",
            "repo": "owner/repo",
            "mode": "head",
            "profile": "python-pytest",
            "profile_version": 1,
            "status": "passed",
            "requested": {"head": head_sha, "base": None},
            "revisions": {"head_sha": head_sha, "head_repo": "owner/repo", "head_ref": "feature", "base_ref": "main"},
            "trees": {"pr_tree": "0" * 40, "effective_tree": "0" * 40},
            "overlay": {"supplied": False, "applied": False, "sha256": None, "paths": [], "patch_status": "none", "committed": False, "pushed": False},
            "selectors": [],
            "test_dirs": ["tests"],
            "timeout_seconds": 300,
            "requirements_sha256": "0" * 64,
            "timestamps": {"created_at": now, "started_at": now, "finished_at": now, "duration_seconds": 0.1},
            "expires_at": now + 48 * 3600,
            "result": {"status": "passed", "counts": {"collected": 1, "reported": 1, "executed": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "incomplete": 0, "not_run": 0}, "tests": [], "tests_total": 1, "tests_truncated": False},
        }
        server.test_service.store.save({**base_record, "run_id": run_id, "pr": 1})
        server.test_service.store.save({**base_record, "run_id": other_pr_run_id, "pr": 99})

        ar_meta = server.artifact_store.put(
            owner="local",
            repo="owner/repo",
            kind="log",
            name="run.log",
            data=b"ok\n",
            context={"run_id": run_id, "tested_tree": "0" * 40},
        )
        ar_id = ar_meta["artifact_id"]

        cmp_id = "cmp-" + "b" * 32
        cmp_store = CampaignStore(Path(tmp_path / "ws" / "campaigns"))
        cmp_store.save(CampaignRecord(
            campaign_id=cmp_id,
            owner="local",
            repo="owner/repo",
            number=1,
            mode="pr_head",
            status="passed",
            created_at=now,
            updated_at=now,
            finished_at=now,
            requested_head=head_sha,
            validation_executed=True,
            workspace_cleaned=True,
        ))

        ws_before = _snapshot_tree(tmp_path / "ws")
        runs_before = _snapshot_tree(tmp_path / "runs")
        artifacts_before = _snapshot_tree(tmp_path / "artifacts")

        def fail_write(*args, **kwargs):
            raise AssertionError("Storage or Git write attempted during read-only tool call")

        # Fail on Git pushes and all durable storage writes/deletes.
        monkeypatch.setattr(GitWorkspace, "push_fast_forward", fail_write)
        orig_git = GitWorkspace.git

        def guarded_git(self, *args, **kwargs):
            if kwargs.get("write"):
                raise AssertionError("GitWorkspace write=True attempted during read-only tool call")
            return orig_git(self, *args, **kwargs)

        monkeypatch.setattr(GitWorkspace, "git", guarded_git)
        for attr in ("_save", "create", "close", "extend", "attach_run"):
            monkeypatch.setattr(WorkspaceService, attr, fail_write)
        for attr in ("save", "delete"):
            monkeypatch.setattr(RunStore, attr, fail_write)
        for attr in ("mutate", "enqueue", "cancel", "reserve"):
            monkeypatch.setattr(JobService, attr, fail_write)
        for attr in ("put", "delete_for_run"):
            monkeypatch.setattr(ArtifactStore, attr, fail_write)
        for attr in ("save", "_write", "claim", "checkpoint", "update", "finalize", "cancel"):
            monkeypatch.setattr(CampaignStore, attr, fail_write)
        for attr in ("save", "signing_key"):
            monkeypatch.setattr(PlanStore, attr, fail_write)
        monkeypatch.setattr(
            server.test_service,
            "probe_runner",
            lambda: {"state": "ok", "facts": {"operational": True, "profiles": {"python-pytest": {"installed": True}}, "limits": {}}},
        )

        calls = [
            ("pr_evidence", {"repo": "owner/repo", "number": 1}),
            ("ci_diagnose", {"repo": "owner/repo", "number": 1}),
            ("pr_conflicts", {"repo": "owner/repo", "number": 1, "expected_head": head_sha}),
            ("pr_rebase_preview", {"repo": "owner/repo", "number": 1, "expected_head": head_sha, "expected_base": live_base_sha}),
            ("test_run_list", {"repo": "owner/repo", "number": 1, "expected_head": head_sha}),
            ("test_result", {"run_id": run_id}),
            ("workspace_read", {"workspace_id": ws_id, "mode": "file", "path": "src/foo.py"}),
            ("workspace_search", {"workspace_id": ws_id, "pattern": "feature"}),
            ("workspace_diff", {"workspace_id": ws_id}),
            ("workspace_list", {}),
            ("workspace_status", {"workspace_id": ws_id}),
            ("campaign_status", {"campaign_id": cmp_id}),
            ("artifact_get", {"artifact_id": ar_id}),
            ("odytor_capabilities", {"repo": "owner/repo", "probe_runner": False}),
            ("odytor_capabilities", {"repo": "owner/repo", "probe_runner": True}),
        ]
        assert {name for name, _ in calls} == READ_ONLY_ALLOWLIST

        async def exercise():
            async with Client(server) as client:
                results = {}
                for name, kwargs in calls:
                    res = await client.call_tool(name, kwargs, raise_on_error=False)
                    assert not res.is_error, f"{name} returned MCP error: {res}"
                    payload = res.structured_content
                    assert isinstance(payload, dict) and "error" not in payload, f"{name} failed: {payload}"
                    results[(name, kwargs.get("probe_runner"))] = payload
                return results

        results = asyncio.run(exercise())
        assert results[("pr_evidence", None)]["head_sha"] == head_sha
        assert results[("ci_diagnose", None)]["total_failed_jobs"] == 1
        assert results[("pr_conflicts", None)]["conflicting"] is False
        assert results[("pr_rebase_preview", None)]["status"] == "rebased"
        assert results[("test_run_list", None)]["runs"][0]["run_id"] == run_id
        assert results[("test_result", None)]["status"] == "passed"
        assert results[("workspace_read", None)]["lines"][1]["text"] == "feature line 2"
        assert results[("workspace_search", None)]["matches"][0]["path"] == "src/foo.py"
        assert results[("workspace_diff", None)]["changed"] is True
        assert [w["workspace_id"] for w in results[("workspace_list", None)]["workspaces"]] == [ws_id]
        assert results[("workspace_status", None)]["version"] == 1
        assert results[("campaign_status", None)]["campaign_id"] == cmp_id
        assert results[("artifact_get", None)]["text"] == "ok\n"
        assert results[("odytor_capabilities", False)]["capabilities"][0]["operational"] is None
        assert results[("odytor_capabilities", True)]["capabilities"][0]["operational"] is True

        assert _snapshot_tree(tmp_path / "ws") == ws_before
        assert _snapshot_tree(tmp_path / "runs") == runs_before
        assert _snapshot_tree(tmp_path / "artifacts") == artifacts_before

        # Closed/merged PR retention housekeeping on test_result: still zero GitHub writes, zero deletes,
        # zero writes to other stores, and only expires_at is shortened on that PR's run records.
        original_pr = gh_client.get_pr
        monkeypatch.setattr(gh_client, "get_pr", lambda repo, number: {**original_pr(repo, number), "state": "closed", "merged": True})
        saved_runs: list[str] = []
        import json as _json

        def housekeeping_only_save(store_self, rec):
            saved_runs.append(rec["run_id"])
            assert rec["run_id"] == run_id
            assert rec["expires_at"] < base_record["expires_at"]
            target = store_self.path(rec["run_id"])
            target.write_text(_json.dumps(rec, separators=(",", ":")))

        monkeypatch.setattr(RunStore, "save", housekeeping_only_save)
        monkeypatch.setattr(JobService, "mutate", lambda svc_self, rid, change: (
            (lambda rec: (change(rec), housekeeping_only_save(svc_self.store, rec)))(svc_self.store.load(rid))
        ))

        async def call_closed_test_result():
            async with Client(server) as client:
                return await client.call_tool("test_result", {"run_id": run_id}, raise_on_error=False)

        closed_res = asyncio.run(call_closed_test_result())
        assert not closed_res.is_error
        assert closed_res.structured_content["currency"]["pr_state"] == "merged"
        assert saved_runs == [run_id]
        assert server.test_service.store.load(run_id)["expires_at"] < base_record["expires_at"]
        assert server.test_service.store.load(other_pr_run_id)["expires_at"] == base_record["expires_at"]
        assert _snapshot_tree(tmp_path / "ws") == ws_before
        assert _snapshot_tree(tmp_path / "artifacts") == artifacts_before
    finally:
        if server.test_service is not None:
            server.test_service.shutdown()


def test_pr_body_apply_is_idempotent_with_single_patch(tmp_path, monkeypatch) -> None:
    head_sha = "a" * 40
    base_sha = "b" * 40
    pr_state = {
        "number": 5,
        "state": "open",
        "merged": False,
        "draft": True,
        "title": "Draft PR title",
        "head": {"sha": head_sha, "ref": "feat", "repo": {"full_name": "owner/repo"}},
        "base": {"sha": base_sha, "ref": "main", "repo": {"full_name": "owner/repo"}},
        "body": "Initial draft body",
    }
    patch_calls: list[dict] = []

    class DraftPRClient:
        token = "t"
        base_url = "https://api.github.com"
        restricted_repo = None
        principal = "local"

        def get_pr(self, repo: str, number: int) -> dict:
            assert repo == "owner/repo" and number == 5
            return dict(pr_state)

        def request(self, method: str, path: str, **kwargs):
            if method.upper() == "PATCH" and path == "repos/owner/repo/pulls/5":
                payload = kwargs.get("json_data") or kwargs.get("json") or {}
                patch_calls.append({"method": method, "path": path, "payload": payload})
                pr_state["body"] = payload["body"]
                return SimpleNamespace(status_code=200, json=lambda: dict(pr_state))
            raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    @contextlib.contextmanager
    def fake_github_client(cfg, remote=False, *, repo=None):
        yield DraftPRClient()

    monkeypatch.setattr("odytor.server.github_client", fake_github_client)
    monkeypatch.setattr("odytor.server.caller_principal", lambda cfg, remote=False: "local")

    (tmp_path / "req.txt").write_text("pytest==8.3.5\n")
    (tmp_path / "policies.toml").write_text('[repos."owner/repo"]\nrequirements = "req.txt"\ntest_dirs = ["tests"]\n')
    server = create_server(_settings(tmp_path, allow_publish_apply=True))
    try:
        async def run_flow():
            async with Client(server) as client:
                planned = await client.call_tool(
                    "pr_body_plan",
                    {"repo": "owner/repo", "number": 5, "expected_head": head_sha, "body": "Replacement draft body"},
                    raise_on_error=False,
                )
                assert not planned.is_error, planned
                plan_data = planned.structured_content
                apply_args = {
                    "plan_id": plan_data["plan_id"],
                    "confirm": plan_data["confirmation"],
                    "user_authorized": True,
                }
                first = await client.call_tool("pr_body_apply", apply_args, raise_on_error=False)
                second = await client.call_tool("pr_body_apply", apply_args, raise_on_error=False)
                return plan_data, first, second

        plan_data, first, second = asyncio.run(run_flow())
        assert not first.is_error and not second.is_error
        assert len(patch_calls) == 1
        assert patch_calls[0]["payload"] == {"body": "Replacement draft body"}

        first_data = first.structured_content
        assert first_data["plan_id"] == plan_data["plan_id"]
        assert first_data["status"] == "applied"
        assert all(first_data["invariants_verified"].values())

        second_data = second.structured_content
        assert second_data == {
            "plan_id": plan_data["plan_id"],
            "kind": "pr_body",
            "repo": "owner/repo",
            "number": 5,
            "status": "applied",
            "idempotent_replay": True,
            "note": "This plan was already applied; nothing was written again.",
        }
    finally:
        if server.test_service is not None:
            server.test_service.shutdown()
