# Odytor Tool Reference

Odytor exposes 31 MCP tools in total: eight original tools whose names form the stable baseline contract, plus 23 additive tools for rebase preview, run recovery, workspaces, browser runs, campaigns, artifacts, guarded publication, and capability discovery. The additive tools for workspaces, browser runs, artifacts and publication are documented in [workspaces](workspaces.md), [browser](browser.md), [artifacts](artifacts.md) and [publishing](publishing.md); `odytor_capabilities` reports at runtime which of them this deployment can actually use.

`pr_conflicts` accepts `merge_target="live_tip"` to merge against the live target-branch tip instead of the PR API's possibly stale base snapshot; the result always states the merged base (`merged_base_sha`) and `merge_scope`. `test_run` overlay failures now name their stage and line (`diagnostic.stage`, `diagnostic.line`, declared versus found hunk counts).

Each tool is designed specifically for AI agents, featuring bounded memory usage, explicit revision validation, and structured error reporting.

## Tool Summary

| Tool | Mode | Primary Purpose | Key Safety Boundary |
| --- | --- | --- | --- |
| [`pr_evidence`](#pr_evidence) | Read-only | Comprehensive PR metadata, windowed diffs, checks, and files | Bounded responses (256 KiB default); explicit completeness flags |
| [`ci_diagnose`](#ci_diagnose) | Read-only | Correlate failed CI jobs, log excerpts, annotations, and code scanning | Capped at 20 failed jobs per call; bounded log retrieval |
| [`pr_conflicts`](#pr_conflicts) | Read-only | Inspect Git merge conflicts via `git merge-tree` | Runs in ephemeral repository; stage and text conflict regions |
| [`quick_fix`](#quick_fix) | Mutating | Apply an exact patch and append one commit to PR head | Exact revision pinned; push lease on forks; non-executable UTF-8 only |
| [`pr_rebase_preview`](#pr_rebase) | Read-only | Preview a linear rebase onto live base branch | Read-only preview; verifies live base tip |
| [`pr_rebase`](#pr_rebase) | Mutating | Linear rebase preview or apply onto live base branch | Requires explicit approval flags; lease-bound push; verifies live tip |
| [`ci_control`](#ci_control) | Mutating | Trigger workflow reruns, failed job reruns, or dispatches | Ref-based dispatch verification; explicit authorization arguments |
| [`test_run`](#test_run) | Execution | Queue isolated pytest execution against exact PR tree | Executes only on isolated runner VM; rootless network-isolated container |
| [`test_result`](#test_result) | Read-only | Retrieve structured test results and evidence | Access checked against GitHub API; bound to submitting principal |

---

## Tool Specifications

### `pr_evidence`

Collects pull request metadata, bounded diffs, changed files, review comments, status checks, and repository instructions in a single structured snapshot.

#### Purpose and Context Optimization
AI models can quickly exhaust their context windows when requesting large pull request diffs. `pr_evidence` solves this by windowing diffs and truncating large payloads before MCP framing, while providing explicit signals about whether any section was trimmed.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `diff_offset` | integer | `0` | Starting line offset within the complete diff |
| `max_diff_lines` | integer | `500` | Maximum diff lines to return in this window (up to 5,000) |
| `files_page` | integer | `1` | Page number for the list of changed files (100 files per page) |
| `include_file_patches` | boolean | `null` | Include per-file patch text (defaults to true for PRs <= 30 files) |

#### Large Pull Request Windowing
GitHub refuses the complete diff of pull requests exceeding 20,000 lines or 300 files (HTTP 406). In such cases, `pr_evidence` automatically falls back to the files API, setting `diff_source: "files_api"`:
- Up to 3,000 changed files can be listed via `files_page` and `files_next_page`.
- Files whose individual patch was withheld by GitHub are listed under `diff_files_without_patch`.
- Diffs are read in windows using `diff_offset` and `max_diff_lines`. The next window begins at `diff_offset + diff_returned_lines`.
- `diff_has_more` indicates whether further diff lines remain.
- Benchmarks on real-world PRs containing 220 to 330 files and 19,000 to 79,000 changed lines demonstrate window retrieval in approximately 10 seconds per call. A 52,710-line diff can be read completely in 20 successive windows.

#### Completeness Flags and Response Bounding
The server enforces a response budget of 256 KiB before MCP framing (configurable between 16 KiB and 4 MiB via `ODYTOR_MAX_RESPONSE_BYTES`).
- When a response field is trimmed to satisfy the budget, every `*_complete` flag is cleared (`diff_complete: false`, `files_complete: false`, `reviews_complete: false`, `checks_complete: false`).
- The `truncated_fields` array explicitly lists every property that was trimmed.
- Agents should inspect `truncated_fields` and request the trimmed property individually using windowing parameters.

---

### `ci_diagnose`

Correlates failed GitHub Actions workflows, individual job logs, step annotations, and GitHub Advanced Security code scanning alerts against the pull request revision.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `expected_head` | string | `null` | Optional expected head commit SHA to guard against snapshot drift |

#### Operational Behavior
- Inspects at most 20 failed jobs per call to bound download time and memory consumption.
- Downloads individual job logs, extracts relevant error segments, and maps them to workflow steps.
- Retrieves code scanning alerts (CodeQL, third-party analyzers) associated with the PR head revision.
- Reports failures with structured failure classification (e.g., test failure, lint failure, setup error, timeout).

---

### `pr_conflicts`

Inspects genuine Git merge conflicts by performing a local `git merge-tree` analysis in an ephemeral workspace.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `expected_head` | string | *required* | Expected commit SHA of the PR head branch |
| `expected_base` | string | *required* | Expected commit SHA of the target base branch |

#### Operational Behavior
- Fetches full ancestry for both revisions into an isolated, temporary Git workspace.
- Executes `git merge-tree` without checking out repository files or executing repository scripts.
- Categorizes conflicts into text conflicts, binary conflicts, and structural conflicts (e.g., file renamed on one side, modified on the other).
- Provides conflict stages and available conflict marker text regions. Unrelated commit histories fail explicitly with a clear validation error.

---

### `quick_fix`

Validates an exact patch in an isolated index and appends a single commit to the PR head branch.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `expected_head` | string | *required* | Expected commit SHA of the PR head branch |
| `patch` | string | *required* | Unified Git diff representing the change |
| `commit_message` | string | *required* | Commit message for the new commit |

#### Patch Validation Constraints
`quick_fix` applies strict validation before touching repository state:
- Applies only to existing, regular, non-executable UTF-8 files.
- Rejects new file creations, deletions, file renames, mode changes, symlinks, submodules, and binary content.
- Rejects modifications to sensitive paths, including CI workflows, credentials, deployment manifests, lockfiles, and package definitions.
- Default limits: at most 10 files, 1,000 changed lines, and 256 KiB of patch text (overridable via `ODYTOR_QUICK_FIX_MAX_FILES`, `ODYTOR_QUICK_FIX_MAX_CHANGED_LINES`, and `ODYTOR_QUICK_FIX_MAX_PATCH_BYTES`).
- Patched files are capped at 1 MiB in total.
- Preparation uses the Git index in an ephemeral workspace; code under test is never executed during validation.

#### Base Repository versus Contributor Fork Execution
- **Base repository head branch:** Odytor submits the resulting tree using GitHub's GraphQL `createCommitOnBranch` mutation, bound to `expectedHeadOid`. Parent commits and tree digests returned by GitHub are verified.
- **Contributor fork head branch:** Odytor creates a single commit on the verified head revision in an ephemeral workspace, then performs a Git push with an explicit lease:
  `--force-with-lease=<ref>:<expected_head>`.
  Odytor first verifies mathematically that the new commit is an exact single child of `expected_head`. This guarantees the update can only be accepted as a linear fast-forward.
  The contributor fork must be publicly fetchable for anonymous verification.
- **Attribution:** Fork commits are attributed to the authenticated GitHub caller via their `users.noreply.github.com` email address. Commits are not signed by GitHub.
- **Result states:** Successful pushes return `outcome: "applied"`. Rejected pushes return `outcome: "rejected"`. Network or verification ambiguities return `outcome: "unknown"`. Mutations are never automatically retried; callers must inspect the PR state before attempting recovery.

---

### `pr_rebase`

Performs or previews a genuine linear rebase of the PR head branch onto the live base branch.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `expected_head` | string | *required* | Expected commit SHA of the PR head branch |
| `expected_base` | string | *required* | Expected commit SHA of the target base branch |
| `apply` | boolean | `false` | Apply the rebase if true; preview changes if false |
| `allow_history_rewrite` | boolean | `false` | Required confirmation flag when `apply=true` |

#### Operational Behavior
- Previews the rebase by default without modifying branch references.
- `apply=true` requires explicit user confirmation via `allow_history_rewrite=true`.
- Verifies that `expected_base` matches the live tip of the target branch, not merely GitHub's cached PR base snapshot.
- Rejects merge commits and PR branches exceeding 200 commits. Preserves empty commits.
- Uses `--force-with-lease` for pushing rewritten history to the PR branch. If the remote branch moves during calculation, the push is rejected and all local state is discarded.
- Commit signatures on rewritten commits are not preserved.

---

### `ci_control`

Coordinates pull request Actions runs, individual failed job reruns, dispatches, and contributor fork workflow approvals.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `operation` | string | *required* | Operation: `rerun_failed`, `rerun_all`, `rerun_job`, `dispatch`, `approve_fork` |
| `run_id` | integer | `null` | Workflow run ID (required for rerun and approval operations) |
| `job_id` | integer | `null` | Job ID (required for `rerun_job`) |
| `workflow_id` | string/int | `null` | Workflow file name or ID (required for `dispatch`) |
| `ref` | string | `null` | Branch ref for dispatch (dispatched in head repository) |
| `inputs` | object | `null` | Input parameters object for workflow dispatch (use `{}` if empty) |
| `authorize_dispatch_semantics` | boolean | `false` | Required acknowledgment flag for `dispatch` |
| `user_authorized_fork_approval` | boolean | `false` | Required acknowledgment flag for `approve_fork` |

#### Operational Behavior
- `rerun_failed`: Triggers rerun of only failed jobs in an existing workflow run.
- `rerun_all`: Triggers a full rerun of all jobs in the workflow run.
- `rerun_job`: Reruns a single specific failed job.
- `dispatch`: Dispatches a workflow in the PR head repository. Because GitHub workflow dispatch is ref-based rather than SHA-based, callers must acknowledge semantic uncertainty via `authorize_dispatch_semantics=true`.
- `approve_fork`: Approves first-time contributor workflow runs requiring maintainer approval. Requires explicit human authorization confirmed via `user_authorized_fork_approval=true`.

---

### `test_run`

Queues an isolated test suite execution against an exact PR head or prospective merge tree.

> [!IMPORTANT]
> Odytor never executes repository test code directly in the MCP server process. Test execution requires an explicitly configured, isolated test runner host. When no runner is configured, `test_run` returns setup instructions rather than failing silently.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | string | *required* | Repository identifier in `owner/repo` format |
| `number` | integer | *required* | Pull request number |
| `expected_head` | string | *required* | Expected commit SHA of the PR head |
| `mode` | string | `"head"` | Execution mode: `"head"` or `"prospective_merge"` |
| `expected_base` | string | `null` | Target branch tip SHA (required for `mode="prospective_merge"`) |
| `profile` | string | `"python-pytest"` | Test runner profile |
| `selectors` | array | `null` | Repository-relative test paths or pytest node IDs |
| `test_overlay` | string | `null` | Unified diff containing reviewer-provided test additions |

#### Operational Behavior
- `mode="head"` tests the PR head tree as committed.
- `mode="prospective_merge"` computes `git merge-tree` with the live base branch tip and tests the resulting tree. If merge conflicts exist, returns `status: "merge_conflict"` without running tests.
- `test_overlay`: Allows reviewers or AI agents to supply unified diffs (up to 64 KiB, 20 files, 2,000 lines) adding new test cases under configured `test_dirs`. Manifests, configuration files, and code outside `test_dirs` are rejected.
- See [docs/test-runner.md](test-runner.md) for full runner VM configuration details.

---

### `test_result`

Retrieves execution evidence, individual test outcomes, logs, and cache performance for a previously queued test run.

#### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `run_id` | string | *required* | Run identifier returned by `test_run` |

#### Operational Behavior
- Bound to the submitting principal (OAuth identity or local caller).
- Verifies continued repository read permissions through the GitHub API before returning data.
- Returns status (`passed`, `failed`, `setup_error`, `timeout`, `canceled`, `no_tests_executed`).
- Includes overlay digest, tested tree SHA, execution duration, and currency check (`currency.tested_head_is_current_pr_head`).
- Use `wait_seconds` and pass each returned `next_cursor` as `after_event_id` to page live progress. Polling and disconnecting leave the job running. `structured_outcomes_complete` describes the test report, and `log_complete` describes the log window. A bounded retained log may be available through `artifact_get`.
- For direct PR runs, when the GitHub currency check observes that the PR is closed or merged, local retention housekeeping trims `expires_at` on retained run records for that PR to the `ODYTOR_TEST_CLOSED_GRACE_HOURS` window.

### `test_run_list`

Recover a lost direct submission response with `test_run_list(repo, number, expected_head)`. Results are newest first and show only your runs on that exact head. Match the mode, base, selectors, overlay digest and creation time, then use `test_result` with the recovered ID. The lookup starts no execution.

### `validation_campaign`

Submit with `wait=false` (the default), keep the `campaign_id`, and monitor via `campaign_status`. While a child runs, use its `run_id` with `test_result` and start at cursor zero. Choose one explicit selector for `playwright-spec` when policy lists more than one spec. A campaign with no validation plans completes with `green=false`. An optional idempotency key binds the effective request to one campaign for its retention period.

---

## Additive PR Helpers, Workspace, Browser, Publication & Discovery Tools

In addition to the eight original baseline tools (`pr_evidence`, `ci_diagnose`, `pr_conflicts`, `quick_fix`, `pr_rebase`, `ci_control`, `test_run`, `test_result`), OdyTor registers 23 additive tools (31 tools total):

- **PR & Run Helpers (documented above)**: `pr_rebase_preview`, `test_run_list`.
- **Workspaces ([docs/workspaces.md](workspaces.md))**: `workspace_create`, `workspace_read`, `workspace_search`, `workspace_edit`, `workspace_diff`, `workspace_run`, `workspace_list`, `workspace_status`, `workspace_manage`, `test_cancel`.
- **Browser & Artifacts ([docs/browser.md](browser.md), [docs/artifacts.md](artifacts.md))**: `browser_run`, `browser_close`, `artifact_get`.
- **Campaigns**: `validation_campaign`, `campaign_status`, `campaign_cancel`.
- **Guarded Publication ([docs/publishing.md](publishing.md))**: `publish_plan`, `publish_apply`, `pr_body_plan`, `pr_body_apply`.
- **Discovery**: `odytor_capabilities` (separates `installed`, `configured`, `operational`, and `authorized` status per capability).

### MCP Tool Annotations & Local-Only Exceptions

Every one of the 31 registered tools sets a human-readable `title` and all four boolean MCP hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`):

- **Read-only (`readOnlyHint=true`)**: 14 tools (`pr_evidence`, `ci_diagnose`, `pr_conflicts`, `pr_rebase_preview`, `test_run_list`, `test_result`, `workspace_read`, `workspace_search`, `workspace_diff`, `workspace_list`, `workspace_status`, `campaign_status`, `artifact_get`, `odytor_capabilities`). `publish_plan` and `pr_body_plan` make no GitHub writes, but they persist durable plan records in local `PlanStore` storage and are marked `readOnlyHint=false`. On closed or merged PRs, `test_result` trims `expires_at` on retained direct PR run records as local retention housekeeping.
- **Destructive (`destructiveHint=true`)**: 7 tools (`pr_rebase` for history rewrites, `pr_body_apply` for PR body replacement, `ci_control` for CI dispatch/rerun/fork approval, `workspace_manage` for workspace deletion, `test_cancel`, `campaign_cancel`, and `browser_close`).
- **Idempotent (`idempotentHint=true`)**: 19 tools: the 14 read-only tools plus 5 mutating tools whose repeated identical calls have no additional effect (`test_cancel` and `campaign_cancel` for run or campaign cancellation, `browser_close` for session disposal, and `publish_apply` and `pr_body_apply` for one-shot plan application).
- **External vs. local-only (`openWorldHint`)**:
  - `openWorldHint=false` (10 strictly local tools): `workspace_read`, `workspace_search`, `workspace_edit`, `workspace_diff`, `workspace_list`, `workspace_status`, `workspace_manage`, `campaign_status`, `browser_close`, `artifact_get`.
  - `openWorldHint=true` (21 tools contacting GitHub or the external runner host `ODYTOR_TEST_RUNNER_COMMAND`), with documented local-only exceptions: `test_result` does not contact GitHub for `workspace` or `browser` runs; `odytor_capabilities` is strictly local under its default `probe_runner=false` (or when no runner is configured); `workspace_run` and `browser_run` never contact GitHub, execute sandboxes with `--network=none` (Chromium only reaches `127.0.0.1`), and return local setup instructions when no runner is configured; `test_cancel` and `campaign_cancel` never contact GitHub and only update local state when canceling queued or already-finished work (or when no runner is active).
