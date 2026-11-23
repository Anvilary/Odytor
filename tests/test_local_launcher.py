"""The shared local launcher (deploy/local/odytor-mcp): credentials, a fixed allowlist, and failing closed.

It runs for real, against a fake `gh` and a fake server executable that reports the environment it was started with.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "deploy" / "local" / "odytor-mcp"
ALLOWED = "Anvilary/*,owner/repo"
TOKEN = "gho_" + "t" * 36


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def sandbox(tmp_path):
    """A checkout whose server prints its environment and arguments, and a gh that answers `auth token`."""
    checkout, bin_dir = tmp_path / "checkout", tmp_path / "bin"
    server = 'echo "ARGS=$*"; env | grep -E "^(ODYTOR|GIT_PLUS)_|^GITHUB_TOKEN=|^GH_TOKEN="'
    executable(checkout / ".venv/bin/odytor", server)
    executable(bin_dir / "gh", f'[ "$1 $2" = "auth token" ] && echo {TOKEN} && exit 0; exit 1')
    return checkout, bin_dir


def run(sandbox, *, env=None, tool="odytor"):
    checkout, bin_dir = sandbox
    if tool == "git-plus":  # an installation made before the rename: only the old command exists
        (checkout / ".venv/bin/odytor").rename(checkout / ".venv/bin/git-plus")
    environment = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(checkout.parent), "ODYTOR_HOME": str(checkout), **(env or {})}
    return subprocess.run(["bash", str(LAUNCHER)], env=environment, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


def started_with(result):
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_server_starts_for_stdio_with_the_token_and_the_fixed_allowlist_under_both_names(sandbox):
    result = run(sandbox)
    seen = started_with(result)
    assert result.returncode == 0 and seen["ARGS"] == "--transport stdio" and seen["GITHUB_TOKEN"] == TOKEN
    assert seen["ODYTOR_ALLOWED_REPOSITORIES"] == seen["GIT_PLUS_ALLOWED_REPOSITORIES"] == ALLOWED
    assert TOKEN not in result.stderr


def test_an_installation_that_predates_the_rename_still_receives_the_allowlist(sandbox):
    """The old command only reads GIT_PLUS_*: starting it without the allowlist would mean unrestricted access."""
    result = run(sandbox, tool="git-plus")
    seen = started_with(result)
    assert result.returncode == 0 and seen["GIT_PLUS_ALLOWED_REPOSITORIES"] == ALLOWED


def test_stray_variables_cannot_widen_the_allowlist_or_select_another_credential(sandbox):
    stray = {"GIT_PLUS_ALLOWED_REPOSITORIES": "evil/repo", "ODYTOR_ALLOWED_REPOSITORIES": "other/repo", "GH_TOKEN": "ghp_stray",
             "ODYTOR_SERVER_PAT": "ghp_pat", "GIT_PLUS_ODYSSEUS_PAT": "ghp_pat2", "GIT_PLUS_AUTH_TOKEN": "static",
             "ODYTOR_GITHUB_CLIENT_ID": "x", "GIT_PLUS_PUBLIC_URL": "https://x.example",
             "GIT_PLUS_WORKSPACE_ALLOW_PUBLISH_APPLY": "true"}
    seen = started_with(run(sandbox, env=stray))
    assert seen["ODYTOR_ALLOWED_REPOSITORIES"] == seen["GIT_PLUS_ALLOWED_REPOSITORIES"] == ALLOWED
    assert seen["GITHUB_TOKEN"] == TOKEN
    assert "ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY" not in seen
    assert "GIT_PLUS_WORKSPACE_ALLOW_PUBLISH_APPLY" not in seen
    allowed_keys = {
        "ARGS", "GITHUB_TOKEN", "ODYTOR_HOME", "ODYTOR_ALLOWED_REPOSITORIES", "GIT_PLUS_ALLOWED_REPOSITORIES",
        "ODYTOR_TEST_RUNNER_COMMAND", "ODYTOR_TEST_POLICIES", "ODYTOR_TEST_DATA_DIR",
        "ODYTOR_WORKSPACE_DIR", "ODYTOR_ARTIFACT_DIR",
    }
    assert not {k for k in seen if k not in allowed_keys}


def test_explicit_publish_apply_opt_in_is_preserved(sandbox):
    seen = started_with(run(sandbox, env={"ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY": "on"}))
    assert seen["ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY"] == "on"


@pytest.mark.parametrize("gh_body,message", [("exit 1", "not authenticated"), ("echo", "returned no token")])
def test_it_fails_closed_without_a_credential_and_keeps_stdout_clean(sandbox, gh_body, message):
    checkout, bin_dir = sandbox
    executable(bin_dir / "gh", gh_body)
    result = run(sandbox)
    assert result.returncode == 1 and result.stdout == "" and message in result.stderr and "odytor-mcp:" in result.stderr


def test_it_fails_closed_when_no_server_is_installed(tmp_path):
    bin_dir = tmp_path / "bin"
    executable(bin_dir / "gh", f"echo {TOKEN}")
    result = subprocess.run(["bash", str(LAUNCHER)], env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "ODYTOR_HOME": str(tmp_path / "missing")},
                            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert result.returncode == 1 and result.stdout == "" and "executable not found" in result.stderr


def test_the_server_process_replaces_the_launcher_so_signals_and_exit_status_pass_through(sandbox):
    checkout, bin_dir = sandbox
    executable(checkout / ".venv/bin/odytor", 'echo "PID=$$"; exit 7')
    result = run(sandbox)
    assert result.returncode == 7  # exec: the launcher's own exit status is the server's
