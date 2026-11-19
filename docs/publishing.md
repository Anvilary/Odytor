# Guarded publication

Publication turns a validated workspace change into **one additive commit on the PR's head branch**, and only after
explicit authorization. It is deliberately two separate acts.

## Plan (`publish_plan`) — read-only

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

## Apply (`publish_apply`) — the only write

It refuses unless **all** of these hold, each re-checked against live state:

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

## Not covered

Branch workspaces and prospective-merge workspaces cannot be published (a merged tree would add merge content to the PR
head; a branch has no review gate). There is no signing of fork-pushed commits.
