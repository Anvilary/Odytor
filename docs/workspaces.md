# Disposable engineering workspaces

A workspace lets an agent inspect, modify and test an exact revision of a repository **without a local checkout and
without changing GitHub**. It is the foundation for the browser tools and for guarded publication.

## The model

A workspace is a private Git object store plus a JSON state record, owned by the authenticated caller. Its working
state is a **tree object** and a **version number**. Nothing is ever checked out: paths exist only as tree entries, so
there is no directory to escape from, no symlink to follow and no file mode to abuse on the control plane. Reads
(`ls-tree`, `cat-file`, `grep <tree>`) and writes (`update-index --cacheinfo`, `write-tree`) go through the same
hardened Git configuration as the rest of Odytor: no inherited configuration, hooks, filters or credentials,
literal pathspecs, and no auto-gc.

Every result echoes `workspace_id`, `version` and `tree`, so the exact state an answer refers to is never ambiguous.

## Tools

| Tool | Purpose |
|---|---|
| `workspace_create` | Open at `pr_head`, `prospective_merge` (against a live target-branch tip) or an exact `branch` revision. Verified against what was actually fetched. |
| `workspace_read` | `tree` (directory listing), `file` (numbered line ranges, at most 400 lines or 64 KiB per call) or `symbols` (Python by parsing, JS/TS by pattern). |
| `workspace_search` | `git grep` over the exact tree; literal by default, extended regex on request; at most 100 matches. |
| `workspace_edit` | `write`, `replace`, `delete`, `rename`, `patch`, `checkpoint`, `reset`. Every call carries `expected_version`. |
| `workspace_diff` | Per-file status, line counts and the patch in pageable windows, plus the digest of the whole patch. |
| `workspace_run` | Run `python-pytest` or `node-test` on the exact current tree. Browser scenarios use `browser_run`. |
| `workspace_manage` | `list`, `status`, `extend`, `close`. |
| `test_result`, `test_cancel` | Poll or cancel a run. |

## Concurrency and consistency

* Every mutation holds an exclusive per-workspace file lock and must name the version it was based on. A stale
  caller receives `VersionConflictError` with the current version and tree and **nothing changes**. Two writers
  starting from one version can never both win (covered by a threaded test).
* An edit that produces an identical tree is not a new version.
* `workspace_run` exports the current tree while holding the lock and the job carries only that tar. A later edit
  cannot change what runs, and the result states the version and tree that was tested and whether it is still the
  workspace's current tree.
* `reset` returns to the source revision or a checkpoint. History is kept and the version still advances, so a reset
  is itself a visible, versioned change.

## Confinement

* Paths are plain repository-relative strings: no absolute paths, `..`, empty or `.` components, backslashes or control
  characters; nothing inside `.git`; `.gitmodules` cannot be written. They never reach a shell, and Git receives them
  after `--` with literal pathspecs.
* Only regular UTF-8 text files can be written. Binary content, symlinks and submodules are refused. Nothing can be
  created beneath a symlink or a regular file. Existing modes (including the executable bit) are preserved.
* Patches use the same strict parser as test overlays: exact hunk counts, regular files only, and every failure names
  its stage and line. Context must match the current content exactly.
* An unknown, foreign or expired `workspace_id` always returns the same error, so ids cannot be probed.

## Limits (all hard caps, configurable)

| Setting | Default |
|---|---|
| `ODYTOR_WORKSPACE_TTL_HOURS` / `_MAX_TTL_HOURS` | 24 h, extendable to 72 h from creation |
| `ODYTOR_WORKSPACE_MAX_PER_OWNER` | 5 |
| `ODYTOR_WORKSPACE_MAX_MIB` / `_MAX_TOTAL_MIB` | 512 MiB each, 4096 MiB overall |
| `ODYTOR_WORKSPACE_MAX_FILE_BYTES` | 1 MiB per edited file |
| `ODYTOR_WORKSPACE_MAX_CHANGED_FILES` | 300 files differing from the source |
| `ODYTOR_WORKSPACE_MAX_CHECKPOINTS` | 20 |
| `ODYTOR_WORKSPACE_DIR` | `$XDG_STATE_HOME/odytor/workspaces` (put it on a persistent volume in production) |
| `ODYTOR_WORKSPACES=off` | disables the feature |

## Authorization scope

Each workspace records `scope = {operations, policy_version}`. `operations` is a subset of `read`, `search`, `edit`,
`diff`, `execute`, `artifacts`, `manage`, `publish`; creating a workspace may only narrow it, and every operation checks
it. **`publish` is not in the default scope**: a workspace can never lead to a GitHub write unless its creator named
`publish` explicitly (see [publishing](publishing.md)). The scope is bound to the caller, repository, workspace and
lifetime; the repository allowlist is re-checked on every call.

## Lifecycle, cleanup and recovery

* State is one atomically replaced JSON file per workspace; the object store holds immutable objects. A restart loses
  nothing; a second Odytor process on the same directory is safe because mutations use cross-process file locks.
* Expired workspaces are removed lazily (at most once a minute) and on creation. A creation that crashed before its
  state file was written leaves a directory without state, which is removed after an hour.
* `workspace_manage(action="close")` deletes the workspace immediately and reports whether it held unpublished changes.
* Runs reference their workspace by id; after a workspace closes or expires the run stays readable and reports
  `workspace_state: closed_or_expired`.
