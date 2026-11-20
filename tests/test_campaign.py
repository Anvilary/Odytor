"""Validation campaign tests: end-to-end orchestration, revision pinning, boundary enforcement, and disposal."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from odytor.campaign import CampaignStore, run_validation_campaign
from odytor.config import RunSettings, Settings, WorkspaceSettings
from odytor.errors import SecurityError, ValidationError
from odytor.profile import RepoPolicy
from odytor.workspace import WorkspaceService
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401


OWNER = "test-operator"


@pytest.fixture
def test_env(tmp_path, local_fetch):
    repo, sha = local_fetch
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests/test_demo.py").write_text("def test_ok():\n    assert True\n")
    run(repo, "add", ".")
    run(repo, "commit", "-qm", "add demo test")
    head = run(repo, "rev-parse", "HEAD").decode().strip()
    client = client_for(head)

    ws_service = WorkspaceService(
        WorkspaceSettings(data_dir=str(tmp_path / "ws"), min_free_bytes=64 * 1024 * 1024)
    )
    store = CampaignStore(tmp_path / "campaigns")

    policies = {
        "owner/repo": RepoPolicy(
            repo="owner/repo",
            profile="python-pytest",
            test_dirs=("tests",),
            requirements="",
            requirements_sha256="",
            extra={"node-test": {}},
        )
    }

    mock_job_service = MagicMock()
    mock_job_service.reserve.return_value = None
    mock_job_service.spool_dir.return_value = tmp_path / "spool"
    (tmp_path / "spool").mkdir(exist_ok=True)
    mock_job_service.clock.return_value = 1000.0

    cfg = Settings(
        github_token="t",
        github_api_url="https://api.github.com",
        auth_token=None,
        default_max_diff_lines=500,
        default_max_diff_bytes=1048576,
        allowed_repositories=("owner/repo",),
        workspaces=WorkspaceSettings(data_dir=str(tmp_path / "ws")),
        testing=RunSettings(
            runner_command=("mock_runner",),
            policies_path=str(tmp_path / "policies.toml"),
            data_dir=str(tmp_path / "runs"),
        ),
    )

    return {
        "client": client,
        "head": head,
        "ws_service": ws_service,
        "store": store,
        "policies": policies,
        "job_service": mock_job_service,
        "cfg": cfg,
        "tmp_path": tmp_path,
    }


def test_campaign_orchestration_and_guaranteed_cleanup(test_env, monkeypatch):
    client = test_env["client"]
    head = test_env["head"]
    ws_service = test_env["ws_service"]
    store = test_env["store"]
    policies = test_env["policies"]
    service = test_env["job_service"]
    cfg = test_env["cfg"]

    # Mock wait_for_run to report passed run
    monkeypatch.setattr(
        "odytor.campaign.wait_for_run",
        lambda svc, run_id, timeout: {
            "status": "passed",
            "failure": None,
            "counts": {"passed": 1, "failed": 0},
            "artifacts": [{"name": "test.log", "kind": "log"}],
        },
    )

    result = run_validation_campaign(
        client,
        "owner/repo",
        1,
        expected_head=head,
        mode="pr_head",
        profiles=["python-pytest"],
        workspaces=ws_service,
        service=service,
        sessions=None,
        policies=policies,
        owner=OWNER,
        cfg=cfg,
        campaign_store=store,
    )

    assert result["status"] == "passed"
    assert result["tested_head"] == head
    assert result["workspace_cleaned"] is True
    assert result["github_mutations"] == "none"
    assert len(result["steps"]) >= 2  # create, profile, cleanup
    assert result["results"]["python-pytest"]["status"] == "passed"

    # Verify workspace was closed and is no longer accessible
    with pytest.raises(ValidationError):
        ws_service.get(result["steps"][0]["workspace_id"], OWNER, operation="read")


def test_campaign_fails_on_unauthorized_policy(test_env):
    client = test_env["client"]
    head = test_env["head"]
    ws_service = test_env["ws_service"]
    store = test_env["store"]
    policies = test_env["policies"]
    service = test_env["job_service"]
    cfg = test_env["cfg"]

    # Attempting a profile not enabled in policy (e.g. make-build)
    with pytest.raises(ValidationError) as exc_info:
        run_validation_campaign(
            client,
            "owner/repo",
            1,
            expected_head=head,
            mode="pr_head",
            profiles=["make-build"],
            workspaces=ws_service,
            service=service,
            sessions=None,
            policies=policies,
            owner=OWNER,
            cfg=cfg,
            campaign_store=store,
        )
    assert "not enable profile 'make-build'" in str(exc_info.value)


def test_campaign_cancels_cleanly_via_store(test_env):
    store = test_env["store"]
    service = test_env["job_service"]
    ws_service = test_env["ws_service"]

    # Create dummy campaign record in store
    rec = store._path("cmp-test123")
    rec.write_text(
        json.dumps({
            "campaign_id": "cmp-test123",
            "owner": OWNER,
            "repo": "owner/repo",
            "number": 1,
            "mode": "pr_head",
            "status": "running",
            "created_at": 100.0,
            "updated_at": 100.0,
            "workspace_id": None,
            "workspace_cleaned": False,
        })
    )

    res = store.cancel("cmp-test123", OWNER, service=service, workspaces=ws_service)
    assert res["status"] == "canceled"
    loaded = store.load("cmp-test123", OWNER)
    assert loaded.status == "canceled"


def test_empty_profiles_does_not_silently_run_pytest(test_env, monkeypatch):
    client = test_env["client"]
    head = test_env["head"]
    ws_service = test_env["ws_service"]
    store = test_env["store"]
    policies = test_env["policies"]
    service = test_env["job_service"]
    cfg = test_env["cfg"]

    called_runs = []
    monkeypatch.setattr(
        "odytor.campaign.submit_workspace_run",
        lambda *args, **kwargs: called_runs.append(kwargs.get("profile")),
    )

    result = run_validation_campaign(
        client,
        "owner/repo",
        1,
        expected_head=head,
        mode="pr_head",
        profiles=[],  # explicitly empty
        workspaces=ws_service,
        service=service,
        sessions=None,
        policies=policies,
        owner=OWNER,
        cfg=cfg,
        campaign_store=store,
    )
    assert result["status"] == "passed"
    assert len(called_runs) == 0
    assert result["results"] == {}
    assert result["tested_head"] == head
    assert result["workspace_cleaned"] is True


def test_setup_error_does_not_label_requested_revisions_as_tested(test_env):
    from odytor.errors import RevisionMismatchError
    client = test_env["client"]
    ws_service = test_env["ws_service"]
    store = test_env["store"]
    policies = test_env["policies"]
    service = test_env["job_service"]
    cfg = test_env["cfg"]

    # PR head mismatch -> setup error before workspace creation
    with pytest.raises((RevisionMismatchError, ValidationError)):
        run_validation_campaign(
            client,
            "owner/repo",
            1,
            expected_head="0000000000000000000000000000000000000000",
            mode="pr_head",
            profiles=["python-pytest"],
            workspaces=ws_service,
            service=service,
            sessions=None,
            policies=policies,
            owner=OWNER,
            cfg=cfg,
            campaign_store=store,
        )

    # No campaign was created / saved with tested_head set
    for camp_file in store.root.glob("cmp-*.json"):
        data = json.loads(camp_file.read_text())
        assert data.get("tested_head") is None
