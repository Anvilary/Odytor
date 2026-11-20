"""Odytor naming and the temporary GIT_PLUS_* compatibility: names, fail-closed conflicts, entry points, MCP identity."""
from __future__ import annotations

import asyncio
import json
import os
from importlib import metadata
from pathlib import Path
from unittest import mock

import pytest

from odytor import __version__, envvars, sandbox
from odytor.cli import startup_notes
from odytor.config import RunSettings, Settings
from odytor.envvars import EnvConflictError, default_state_dir, legacy_names_in_use, lookup, with_prefix
from odytor.errors import ValidationError
from odytor.server import create_server

PAT = "ghp_" + "p" * 36
# A production-style legacy deployment: every setting under the old prefix, as the existing .env has them.
LEGACY = {"GIT_PLUS_PUBLIC_URL": "https://git-plus.example.com", "GIT_PLUS_GITHUB_CLIENT_ID": "cid", "GIT_PLUS_GITHUB_CLIENT_SECRET": "csecret",
          "GIT_PLUS_OAUTH_SIGNING_KEY": "k" * 32, "GIT_PLUS_ALLOWED_GITHUB_USERS": "Maintainer",
          "GIT_PLUS_ALLOWED_REPOSITORIES": "odysseus-dev/odysseus,Anvilary/git-plus,me/site",
          "GIT_PLUS_ODYSSEUS_PAT": PAT, "GIT_PLUS_ODYSSEUS_PAT_LOGIN": "maintainer"}


def clean_env(**extra):
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_PLUS_", "ODYTOR_"))}
    return mock.patch.dict(os.environ, {**base, **extra}, clear=True)


def test_new_name_wins_alone_legacy_works_alone_and_equal_values_are_fine():
    assert lookup("X", {"ODYTOR_X": "new"}) == "new" and lookup("X", {"GIT_PLUS_X": "old"}) == "old" and lookup("X", {}) is None
    assert lookup("X", {"ODYTOR_X": "same", "GIT_PLUS_X": "same"}) == "same"


@pytest.mark.parametrize("new,old", [("a/b", "c/d"), ("", "x"), ("x", "")])
def test_conflicting_values_fail_closed_instead_of_choosing(new, old):
    with pytest.raises(EnvConflictError, match="remove GIT_PLUS_ALLOWED_REPOSITORIES"):
        lookup("ALLOWED_REPOSITORIES", {"ODYTOR_ALLOWED_REPOSITORIES": new, "GIT_PLUS_ALLOWED_REPOSITORIES": old})


def test_a_production_style_legacy_environment_still_configures_everything():
    with clean_env(**LEGACY):
        legacy = Settings.load()
    with clean_env(**{k.replace("GIT_PLUS_", "ODYTOR_"): v for k, v in LEGACY.items()}):
        canonical = Settings.load()
    assert legacy == canonical
    assert legacy.allowed_repositories == ("odysseus-dev/odysseus", "anvilary/git-plus", "me/site")  # the allowlist still binds
    assert legacy.is_repo_allowed("Me/Site") and not legacy.is_repo_allowed("evil/repo")
    assert legacy.server_pat_credential().token == PAT and legacy.server_pat_credential().login == "maintainer"
    assert (legacy.oauth_client_id, legacy.oauth_signing_key) == ("cid", "k" * 32)


def test_conflict_in_a_security_setting_stops_startup_rather_than_widening_access():
    with clean_env(ODYTOR_ALLOWED_REPOSITORIES="only/this", GIT_PLUS_ALLOWED_REPOSITORIES="only/this,and/that"):
        with pytest.raises(ValidationError, match="both set with different values"):
            Settings.load()
    with clean_env(ODYTOR_ALLOWED_GITHUB_USERS="alice", GIT_PLUS_ALLOWED_GITHUB_USERS="alice,mallory"):
        with pytest.raises(ValidationError):
            Settings.load()


def test_runner_settings_accept_both_spellings_and_reject_conflicts():
    image = "a" * 64
    legacy = sandbox.RunnerConfig.load({"GIT_PLUS_RUNNER_IMAGE": image, "GIT_PLUS_RUNNER_MEMORY_MIB": "2048", "HOME": "/h"})
    modern = sandbox.RunnerConfig.load({"ODYTOR_RUNNER_IMAGE": image, "ODYTOR_RUNNER_MEMORY_MIB": "2048", "HOME": "/h"})
    assert legacy == modern and legacy.memory_mib == 2048
    with pytest.raises(sandbox.SetupError, match="both set"):
        sandbox.RunnerConfig.load({"ODYTOR_RUNNER_IMAGE": image, "GIT_PLUS_RUNNER_IMAGE": "b" * 64})
    with pytest.raises(sandbox.SetupError, match="ODYTOR_RUNNER_MEMORY_MIB must be between"):
        sandbox.RunnerConfig.load({"ODYTOR_RUNNER_IMAGE": image, "ODYTOR_RUNNER_MEMORY_MIB": "1"})


def test_runner_config_file_may_use_either_prefix_and_still_has_to_be_root_owned(tmp_path, monkeypatch):
    path = tmp_path / "runner.env"
    path.write_text("# managed\nGIT_PLUS_RUNNER_IMAGE=" + "a" * 64 + "\nODYTOR_RUNNER_CPUS=1\n")
    real_stat = os.stat
    monkeypatch.setattr(sandbox.os, "stat", lambda p, *a, **k: type("S", (), {"st_uid": 0, "st_mode": 0o100644})() if str(p) == str(path) else real_stat(p, *a, **k))
    assert sandbox.load_config_file(str(path)) == {"ODYTOR_RUNNER_IMAGE": "a" * 64, "ODYTOR_RUNNER_CPUS": "1"}
    path.write_text("GIT_PLUS_RUNNER_CPUS=1\nODYTOR_RUNNER_CPUS=2\n")
    with pytest.raises(sandbox.SetupError, match="both set"):
        sandbox.load_config_file(str(path))
    path.write_text("PATH=/evil\n")
    with pytest.raises(sandbox.SetupError, match="Invalid runner configuration line 1"):
        sandbox.load_config_file(str(path))
    monkeypatch.setattr(sandbox.os, "stat", real_stat)
    with pytest.raises(sandbox.SetupError, match="owned by root"):
        sandbox.load_config_file(str(path))


def test_runner_subprocess_receives_both_prefixes_but_never_github_credentials():
    from odytor.jobs import runner_environment
    with clean_env(ODYTOR_RUNNER_IMAGE="x", GIT_PLUS_RUNNER_CPUS="1", GITHUB_TOKEN="secret", ODYTOR_SERVER_PAT="p", PATH="/usr/bin"):
        env = runner_environment()
    assert env["ODYTOR_RUNNER_IMAGE"] == "x" and env["GIT_PLUS_RUNNER_CPUS"] == "1"
    assert "GITHUB_TOKEN" not in env and "ODYTOR_SERVER_PAT" not in env


def test_state_directories_keep_pointing_at_existing_data(tmp_path):
    state = str(tmp_path)
    assert default_state_dir(state, "odytor", "git-plus", "tests") == str(tmp_path / "odytor" / "tests")  # fresh install
    (tmp_path / "git-plus" / "tests").mkdir(parents=True)
    assert default_state_dir(state, "odytor", "git-plus", "tests") == str(tmp_path / "git-plus" / "tests")  # existing data wins
    (tmp_path / "odytor" / "tests").mkdir(parents=True)
    assert default_state_dir(state, "odytor", "git-plus", "tests") == str(tmp_path / "odytor" / "tests")  # once migrated


def test_runner_root_defaults_to_the_legacy_directory_when_it_holds_a_cache(tmp_path):
    image = "a" * 64
    assert sandbox.RunnerConfig.load({"ODYTOR_RUNNER_IMAGE": image, "HOME": str(tmp_path)}).root == tmp_path / ".local/state/odytor-runner"
    (tmp_path / ".local/state/git-plus-runner/envs").mkdir(parents=True)
    assert sandbox.RunnerConfig.load({"ODYTOR_RUNNER_IMAGE": image, "HOME": str(tmp_path)}).root == tmp_path / ".local/state/git-plus-runner"


def test_start_up_notes_flag_legacy_names_without_printing_values():
    with clean_env(GIT_PLUS_ALLOWED_REPOSITORIES="a/b", GIT_PLUS_SERVER_PAT=PAT, ODYTOR_PUBLIC_URL="u"):
        notes = " ".join(startup_notes(Settings.load()))
    assert "GIT_PLUS_" in notes and "ODYTOR_" in notes and PAT not in notes
    assert legacy_names_in_use({"GIT_PLUS_A": "1", "GIT_PLUS_B": "1", "ODYTOR_B": "1"}) == ["GIT_PLUS_A"]


def test_with_prefix_canonicalizes_and_detects_conflicts():
    assert with_prefix({"GIT_PLUS_RUNNER_A": "1", "ODYTOR_RUNNER_B": "2", "OTHER": "3"}, "RUNNER_") == {"ODYTOR_RUNNER_A": "1", "ODYTOR_RUNNER_B": "2"}
    with pytest.raises(EnvConflictError):
        with_prefix({"GIT_PLUS_RUNNER_A": "1", "ODYTOR_RUNNER_A": "2"}, "RUNNER_")


def test_canonical_commands_and_temporary_aliases_point_at_the_same_functions():
    scripts = {e.name: e.value for e in metadata.entry_points(group="console_scripts") if e.dist and e.dist.name == "odytor"}
    assert scripts["odytor"] == scripts["git-plus"] == "odytor.cli:main"
    assert scripts["odytor-runner"] == scripts["git-plus-runner"] == "odytor.sandbox:main"
    assert metadata.version("odytor") == __version__ == "0.1.0"


def test_mcp_identity_is_odytor_and_keeps_the_original_eight_tools():
    settings = Settings(github_token="t", github_api_url="https://api.github.com", auth_token=None, default_max_diff_lines=500,
                        default_max_diff_bytes=1048576, allowed_repositories=())
    server = create_server(settings)
    assert server.name == "Odytor"
    tools = {t.name: t.annotations.model_dump(exclude_none=True, by_alias=True) for t in asyncio.run(server.list_tools())}
    ORIGINAL = {"pr_evidence", "ci_diagnose", "pr_conflicts", "quick_fix", "pr_rebase", "ci_control", "test_run", "test_result"}
    assert ORIGINAL <= set(tools)  # the original eight stay, by name
    assert set(tools) - ORIGINAL == {"odytor_capabilities", "browser_run", "browser_close", "artifact_get", "publish_plan", "publish_apply", "workspace_create", "workspace_read", "workspace_search", "workspace_edit", "workspace_diff", "workspace_run", "workspace_manage", "test_cancel", "pr_rebase_preview", "validation_campaign", "campaign_status", "campaign_cancel", "pr_body_plan", "pr_body_apply"}
    assert tools["pr_evidence"]["readOnlyHint"] is True and tools["pr_rebase"]["destructiveHint"] is True
    assert tools["quick_fix"]["readOnlyHint"] is False and tools["test_result"]["readOnlyHint"] is True


def test_no_legacy_brand_remains_in_user_facing_runtime_strings():
    root = Path(__file__).resolve().parents[1] / "odytor"
    allowed = {"envvars.py", "sandbox.py", "config.py", "jobs.py", "cli.py"}  # the compatibility layer and legacy runtime identifiers
    for source in root.glob("*.py"):
        if source.name in allowed:
            continue
        text = source.read_text()
        assert "Git Plus" not in text and "GIT_PLUS" not in text, source.name
