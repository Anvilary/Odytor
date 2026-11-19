"""Runtime capability discovery: what this deployment can actually do, with evidence for each claim.

Each capability reports four separate facts so a caller never mistakes one for another:
  installed   the code for it is present in this build
  configured  the operator provided what it needs (runner command, policy, ...)
  operational observed working just now (a live probe), or None when it has not been observed
  authorized  the calling principal may use it (repository allowlist, test policy)

Nothing here executes repository code, and nothing secret is reported: no tokens, credentials, hostnames,
filesystem paths or runner command lines.
"""
from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path
from typing import Any

from odytor import __version__
from odytor.config import Settings
from odytor import browser_spec
from odytor.profile import BROWSER_PROFILE, REGISTRY, RepoPolicy

# Tools that cannot work without a configured runner; the server registers them either way and explains why.
RUNNER_TOOLS = ("test_run", "test_result", "workspace_run", "test_cancel", "browser_run")
WORKSPACE_TOOLS = ("workspace_create", "workspace_read", "workspace_search", "workspace_edit", "workspace_diff",
                   "workspace_run", "workspace_manage", "publish_plan", "publish_apply")
RUNNER_PROTOCOL = 1


@functools.lru_cache(maxsize=1)
def source_revision() -> dict[str, Any]:
    """Build identity: operator-stamped values win; a development checkout falls back to its own git HEAD."""
    stamped = (os.environ.get("ODYTOR_BUILD_REVISION") or "").strip().lower()
    build_id = (os.environ.get("ODYTOR_BUILD_ID") or "").strip()[:64] or None
    if len(stamped) == 40 and all(c in "0123456789abcdef" for c in stamped):
        return {"revision": stamped, "source": "build_stamp", "build_id": build_id}
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists():
        env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        try:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], env=env, capture_output=True, text=True, timeout=5)
            dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], env=env,
                                   capture_output=True, text=True, timeout=5)
            if head.returncode == 0 and len(head.stdout.strip()) == 40:
                return {"revision": head.stdout.strip(), "source": "git_checkout", "build_id": build_id,
                        "working_tree_dirty": bool(dirty.stdout.strip())}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"revision": None, "source": "unknown", "build_id": build_id,
            "note": "No build stamp. Set ODYTOR_BUILD_REVISION (40-hex commit) when building the image."}


def _capability(name: str, *, installed: bool, configured: bool, operational: bool | None, authorized: bool | None,
                reason: str | None = None, **extra: Any) -> dict[str, Any]:
    available = installed and configured and operational is not False and authorized is not False
    row = {"name": name, "installed": installed, "configured": configured, "operational": operational,
           "authorized": authorized, "available": available}
    if not available:
        row["reason"] = reason or ("not configured" if not configured else "not operational" if operational is False else "not authorized")
    row.update(extra)
    return row


def describe(cfg: Settings, *, policies: dict[str, RepoPolicy], service: Any | None, remote: bool, principal: str | None,
             repo: str | None = None, probe: bool = False, operations: tuple[str, ...] = (),
             workspaces_enabled: bool = False, runner_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the report. `runner_facts` (a probe result) is supplied only when the caller asked for a live probe."""
    runs = cfg.testing
    repo_key = repo.strip().casefold() if repo else None
    repo_allowed = cfg.is_repo_allowed(repo) if repo else None
    policy = policies.get(repo_key) if repo_key else None
    if runs is None:
        runner_state: dict[str, Any] = {"state": "not_configured"}
    elif runner_facts is not None:
        runner_state = runner_facts
    else:
        runner_state = {"state": "not_probed", "hint": "Pass probe_runner=true to observe the runner now."}
    operational = None if runs is None else (runner_state.get("state") == "ok" if runner_facts is not None else None)
    facts = runner_state.get("facts") or {}
    execution = _capability(
        "isolated_execution", installed=True, configured=runs is not None, operational=operational,
        authorized=(None if repo is None else bool(repo_allowed and policy is not None)),
        reason=("Isolated execution is not configured on this server (ODYTOR_TEST_RUNNER_COMMAND, ODYTOR_TEST_POLICIES)."
                if runs is None else
                (f"Repository '{repo}' is not permitted by this server." if repo_allowed is False else
                 f"Repository '{repo}' has no maintainer test policy." if repo and policy is None else
                 "The runner did not pass its live probe." if operational is False else None)),
        runner=runner_state)
    workspace = _capability(
        "workspaces", installed=True, configured=workspaces_enabled, operational=None,
        authorized=(None if repo is None else bool(repo_allowed)),
        reason=("Workspaces are disabled on this server (ODYTOR_WORKSPACES=off)." if not workspaces_enabled else
                f"Repository '{repo}' is not permitted by this server." if repo_allowed is False else None),
        executes_code=False, tools=[t for t in WORKSPACE_TOOLS if t in operations and t != "workspace_run"])
    runner_profiles = facts.get("profiles") if isinstance(facts.get("profiles"), dict) else {}
    profiles = []
    for name, spec in sorted(REGISTRY.items()):
        seen = runner_profiles.get(name) if isinstance(runner_profiles.get(name), dict) else {}
        row = {"name": name, "version": spec.version, "kind": spec.kind, "runtime": spec.runtime, "summary": spec.summary,
               "commands": list(spec.commands), "network": "none (the sandbox has no network; only pytest dependency preparation does)"
               if spec.needs_env else "none", "needs_tools": list(spec.tools),
               "installed_on_runner": seen.get("installed") if "installed" in seen else None,
               "missing_tools": seen.get("missing_tools", []) if "installed" in seen else None,
               "enabled_for_repo": None if policy is None else name in policy.profiles}
        profiles.append(row)
    browser_installed = (runner_profiles.get(BROWSER_PROFILE) or {}).get("installed") if runner_profiles else None
    browser = _capability(
        "browser", installed=True, configured=runs is not None and (policy is None or BROWSER_PROFILE in policy.profiles),
        operational=(browser_installed if (runner_facts is not None and runner_state.get("state") == "ok") else None),
        authorized=(None if repo is None else bool(repo_allowed and policy is not None and BROWSER_PROFILE in policy.profiles)),
        reason=("The isolated runner is not configured." if runs is None else
                f"The maintainer policy does not enable browser-playwright for '{repo}'." if policy is not None and BROWSER_PROFILE not in policy.profiles else
                "The runner image lacks Chromium or Playwright." if browser_installed is False else None),
        engine="chromium", executes_code=True, isolation="container: no network, read-only root, no capabilities",
        limits={"max_new_steps": browser_spec.MAX_STEPS, "max_session_steps": browser_spec.MAX_SESSION_STEPS,
                "max_screenshots_per_run": browser_spec.MAX_SCREENSHOTS, "max_mocks": browser_spec.MAX_MOCKS},
        not_implemented=["unrestricted JavaScript evaluation", "video", "file upload and download", "external origins"])
    pub_apply_enabled = workspaces_enabled and bool(getattr(getattr(cfg, "workspaces", None), "allow_publish_apply", False))
    publication = _capability(
        "publication", installed=True, configured=pub_apply_enabled, operational=None,
        authorized=(None if repo is None else bool(repo_allowed)),
        reason=("Workspaces are disabled on this server." if not workspaces_enabled else
                "Publication apply is disabled on this server deployment (publish_plan is available for review)." if not pub_apply_enabled else
                f"Repository '{repo}' is not permitted by this server." if repo_allowed is False else None),
        writes_to_github=pub_apply_enabled, requires=["a pr_head workspace with the 'publish' operation in its scope", "publish_plan, then explicit user "
                                           "authorization, then publish_apply"],
        guarantees=["one additive commit on the PR head", "no force push, merge, approval or comment", "plan bound to exact tree, patch digest "
                    "and expected head", "an uncertain outcome is reconciled against the remote before any retry"])
    no_runner = runs is None
    unavailable = [{"name": n, "reason": "isolated runner not configured"} for n in RUNNER_TOOLS if no_runner and n in operations]
    unavailable += [{"name": n, "reason": "workspaces are disabled"} for n in WORKSPACE_TOOLS
                    if not workspaces_enabled and n in operations and n != "workspace_run"]
    return {
        "odytor": {"version": __version__, "license": "PolyForm-Noncommercial-1.0.0", **source_revision()},
        "transport": "remote" if remote else "local_stdio",
        "caller": {"principal": principal, "repository": repo, "repository_permitted": repo_allowed,
                   "test_policy_present": None if repo is None else policy is not None},
        "operations": sorted(operations),
        "operations_unavailable": unavailable,
        "capabilities": [execution, workspace, browser, publication],
        "execution_profiles": profiles,
        "runner_protocol": RUNNER_PROTOCOL,
        "limits": {
            "response_bytes": cfg.max_response_bytes,
            "diff": {"default_lines": cfg.default_max_diff_lines, "default_bytes": cfg.default_max_diff_bytes},
            "quick_fix": {"max_files": cfg.quick_fix_max_files, "max_changed_lines": cfg.quick_fix_max_changed_lines,
                          "max_patch_bytes": cfg.quick_fix_max_patch_bytes},
            **({"runs": {"max_queued": runs.max_queued, "default_timeout_seconds": runs.default_timeout,
                         "max_timeout_seconds": runs.max_timeout, "retention_hours": runs.retention_hours,
                         "max_source_mib": runs.max_source_bytes // (1024 * 1024), "max_overlay_bytes": runs.max_overlay_bytes},
                "runner": facts.get("limits")} if runs is not None else {})},
        "health": ({"queue_depth": service.queue_depth()} if service is not None else {}),
        "artifact_formats": {"native_image": ["image/png", "image/jpeg", "image/webp"], "text_window": ["text/plain"],
                             "base64_chunks": ["application/zip (Playwright traces)", "other binary"],
                             "never_rendered": ["text/html", "image/svg+xml", "scripts"]},
        "known_unavailable": [
            ],
    }
