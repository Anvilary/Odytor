# Guarded publication

Publication turns a validated workspace change into **one additive commit on the PR's head branch**, and only after
explicit authorization. It is deliberately two separate acts.

## Plan (`publish_plan`) - read-only

Requires a workspace opened with `source="pr_head"` **and** with `publish` in its scope (the default scope does not
include it). It writes nothing to GitHub. The plan binds:

* the repository, the PR and the target branch, with the **expected remote head**;
* the exact workspace version and tree, and the base tree;
* the **complete patch**, identified by `patch_sha256` and `patch_bytes` (inline when small, otherwise pageable through
  `workspace_diff`, whose digest is identical);
* the commit message;
* the validation runs recorded **for that very tree** (runs on other trees are counted, never mixed in);
* the known limitations, for example that no test run exists for this tree;
* the required authorization: `user_authorized=true` and a `confirm` text that names the plan, the tree and the patch
  digest.

Every changed path must pass the same write policy `quick_fix` enforces (no workflow or CI files, dependency manifests,
scripts, key material, container definitions or sensitive names), and the change must stay within the configured
`quick_fix` limits for files, lines and bytes. Only regular text files can be added, modified or deleted; mode changes,
symlinks, submodules and binaries are refused. A plan expires after an hour.

## Apply (`publish_apply`) - the only workspace commit write

Requires the server flag `ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY=on` (disabled by default; `publish_plan` remains usable for review when apply is disabled). That flag gates `publish_apply` and `pr_body_apply` only; direct PR maintenance tools (`quick_fix`, `pr_rebase`, `ci_control`) have their own allowlist, revision-pinning, and confirmation guards. `publish_apply` refuses unless **all** of these hold, each re-checked against live state:

1. `user_authorized` is exactly `true` and `confirm` equals the plan's text. This is the server's contract: the user's
   approval is the caller's to obtain, and the confirmation ties it to the immutable inputs they approved.
2. The workspace still holds the planned version and tree.
3. The recomputed patch digest and changed-file list equal the plan's.
4. The PR is still open, still targets the planned head repository and branch, and its head is still the expected head.
5. The path policy still passes.

Then one commit is appended: for a same-repository branch through GitHub's atomic `expectedHeadOid` check (the commit
is created by GitHub); for a contributor fork through a leased fast-forward push with the authenticated user's
attribution and GitHub-confirmed maintainer access (`workspace_publish` is an allowed fork-write operation next to
`quick_fix`, and nothing broader). The response is verified: one parent equal to the expected head and a tree equal to
the planned tree.

It never force-pushes, rewrites history, merges, approves, comments or dispatches workflows, and tests passing never
triggers it.

## Retries and uncertain outcomes

A plan applies **at most once**. Intent is recorded durably before the write. A repeated call returns the earlier
result (`idempotent_replay`). If a write's outcome is uncertain (lost response, timeout, mismatched verification) the plan
becomes `unknown_outcome`; calling `publish_apply` again **reconciles against the real remote first**:

* head is a single child of the expected head with the planned tree: reported as applied, nothing written again;
* head is still the expected head: the write did not land and may be retried;
* anything else: refused with a revision mismatch, nothing written.

A definitive rejection from GitHub leaves the plan pending.

## Draft PR body updates (`pr_body_plan` / `pr_body_apply`)

OdyTor also provides a two-phase flow for updating the description of an **open draft pull request** (`draft=true`):

* `pr_body_plan(repo, number, expected_head, body)` (read-only): verifies that the PR is open and still a draft at `expected_head`, records `current_body_digest` and `replacement_body_digest`, and returns a `pb-*` plan and confirmation phrase.
* `pr_body_apply(plan_id, confirm, user_authorized=false)` (mutating): requires `ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY=on`, `user_authorized=true`, and the exact confirmation text. Before writing, it re-verifies that the PR is open, still a draft, at `expected_head`, and unchanged from `current_body_digest`. Immediately after the `PATCH`, it re-reads the PR to confirm that title, base ref, base SHA, head SHA, draft status, and body all match invariants.

## Not covered

* Branch workspaces and prospective-merge workspaces cannot be published (a merged tree would add merge content to the PR head; a branch has no review gate). There is no signing of fork-pushed commits.
* `pr_body_plan` and `pr_body_apply` refuse non-draft (ready-for-review) pull requests.
* OdyTor v1 does not include PR merge tools, thread reading (`thread_read`), or generalized comment, review verdict, reply, or issue/PR closure tools (`text_plan`, `text_apply`, `text_recover`).
