# Changelog

All notable changes to OdyTor are recorded in this file.

## 0.1.0 - 2026-11-23

First public source release preparation for OdyTor `0.1.0` (`Anvilary/Odytor`).

### Core Pull Request Operations

- `pr_evidence`: Bounded pull request metadata, windowed diffs (with automatic files-API fallback for diffs exceeding GitHub's 20,000-line / 300-file limits), paginated changed-file lists, reviews, status checks, and explicit completeness flags.
- `ci_diagnose`: Correlates failed workflow runs, bounded job logs, step annotations, and GitHub Advanced Security code-scanning alerts against PR revisions.
- `pr_conflicts`: Isolated `git merge-tree` conflict inspection supporting both recorded base snapshots and `merge_target="live_tip"`.
- `quick_fix`: Isolated index validation and single additive commit application on regular non-executable UTF-8 files with path allowlists, GraphQL `createCommitOnBranch` for same-repo PRs, and leased fast-forward pushes for contributor forks.
- `pr_rebase` and `pr_rebase_preview`: Linear rebase preview and lease-guarded rebase execution against the live target branch tip with `apply=true` and `allow_history_rewrite=true` guards.
- `ci_control`: Revision-aware workflow reruns (`rerun_failed`, `rerun_all`, `rerun_job`), dispatches, and fork workflow approvals with explicit confirmation arguments.

### Disposable Engineering Workspaces & Validation

- Checkout-free Git object store workspaces (`workspace_create`, `workspace_read`, `workspace_search`, `workspace_edit`, `workspace_diff`, `workspace_list`, `workspace_status`, `workspace_manage`) with per-workspace locking, `expected_version` concurrency checks, checkpoints, resets, TTL expiry, and scope narrowing.
- Isolated execution (`test_run`, `test_result`, `test_run_list`, `test_cancel`, `workspace_run`, `validation_campaign`, `campaign_status`, `campaign_cancel`) delegating over forced-command SSH to `odytor-runner` using rootless, network-isolated containers.
- Sandboxed headless Chromium execution (`browser_run`, `browser_close`) and owner-bound evidence retrieval (`artifact_get`) delivering screenshots as native MCP image blocks with SHA-256 provenance and chunked base64 fallback.
- Runtime capability discovery (`odytor_capabilities`) reporting `installed`, `configured`, `operational`, and `authorized` states per capability alongside live runner probes.

### Guarded Plan-Then-Apply Publication

- `publish_plan` and `publish_apply`: Two-phase additive commit publication from `pr_head` workspaces opened with `publish` scope, bound to workspace tree, patch digest, expected head, validation evidence, and remote reconciliation on uncertain outcomes.
- `pr_body_plan` and `pr_body_apply`: Two-phase body replacement restricted to open draft pull requests (`draft=true`), with post-write invariant read-back.
- Write execution via `publish_apply` and `pr_body_apply` is gated behind `ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY=on` (disabled by default; `publish_plan` and `pr_body_plan` remain available for read-only planning). Direct PR maintenance mutations (`quick_fix`, `pr_rebase`, `ci_control`) are governed separately by repository allowlists, revision pinning, branch leases, and explicit per-call confirmation flags.

### Intentional 0.1.0 Boundaries

- No pull request merge or branch deletion tools.
- Thread reading (`thread_read`) and generalized text publishing (`text_plan`, `text_apply`, `text_recover`) are deferred/excluded from `0.1.0`.
- Legacy `git-plus` / `git-plus-runner` entry points and `GIT_PLUS_*` configuration aliases remain active in `0.1.0` for backward compatibility (see [docs/migration-from-git-plus.md](docs/migration-from-git-plus.md)).
- Code execution and browser runs require an external isolated runner host (`ODYTOR_TEST_RUNNER_COMMAND`) and maintainer repository policy (`ODYTOR_TEST_POLICIES`).
- Distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE) (source-available for noncommercial use; commercial use requires separate written permission; not OSI-approved open-source software).
