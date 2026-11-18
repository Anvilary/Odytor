# Revision-pinned test runs (`test_run`, `test_result`)

Git Plus can run a repository's pytest suite against an exact PR revision, optionally with a
reviewer-written test overlay, and return structured evidence. Nothing is committed, pushed or posted.

## Architecture and trust boundary

```text
MCP caller -> Git Plus (trusted, holds GitHub credentials)
                 validate caller, repo, PR, exact head/base, selectors, overlay
                 fetch, optional merge-tree, apply overlay to an index, export tree.tar (no credentials)
                 bounded queue (1 worker) ---stdin: header + tree.tar---> git-plus-runner (separate host)
                                                                          validates the job again
                                                                          reuses/builds read-only dependency env
                                                                          rootless container: no network, no credentials
                 <---stdout: bounded JSON evidence---------------------- deletes workspace
```

* **Git Plus never executes repository code.** Execution happens only in `git-plus-runner`, which must run
  on a dedicated runner host or VM with no GitHub tokens, no Git Plus data and no route to production services.
* The runner accepts one thing: a framed job (profile `python-pytest`, validated selectors, approved
  requirements, a tar artifact). It has no shell, no command endpoint and no network listener. In production
  it is reached over SSH with a key restricted to a forced command.
* `git-plus-runner` is the only code that starts containers. Test containers run with `--network=none`,
  `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, user `65534`, pinned image, `--cpus`, `--memory` (no
  swap), `--pids-limit`, size-limited tmpfs for `/tmp` and `/work`, a read-only `/in` and a read-only
  `/opt/env`. No host path is writable and the Docker/Podman socket is never mounted.
* Container isolation is not proof against kernel or runtime escapes. The runner host is the real boundary:
  keep it single-purpose, patched, without credentials and without ingress from or route to production.
* **Result integrity.** Code under test can forge pytest output. Evidence is checked for consistency (exit
  status, session-finish event, per-selector executed counts, spec digest) and never trusted as proof that
  the code is correct; read the diff and the overlay.

## Tool behavior

`test_run(repo, number, expected_head, profile="python-pytest", selectors=None, mode="head", expected_base=None, test_overlay=None)`

* `mode="head"` tests the exact PR head tree. `mode="prospective_merge"` requires `expected_base`, which must
  equal the **live** tip of the target branch (GitHub's cached PR base snapshot is not consulted); it tests
  `git merge-tree` of the exact head and base, and returns `status: merge_conflict` without running tests on
  conflict. Branches that advance during preparation fail with a revision mismatch. A finished run on a
  since-superseded head stays valid and is reported with `currency.tested_head_is_current_pr_head: false`.
* `selectors` are repository-relative pytest paths or node IDs under the repository's configured `test_dirs`.
  They are passed as argv entries after `--`. There are no caller-supplied pytest flags.
* `test_overlay` is a unified diff (default limit 64 KiB, 20 files, 2000 lines) that may add or modify regular
  non-executable files under `test_dirs` with extensions `.py .txt .json .yaml .yml .csv .md .toml`. Rejected:
  deletions, renames, mode changes, symlinks, binary content, manifests and lockfiles, `pytest.ini`/`tox.ini`,
  `sitecustomize.py`, `.pth` files, workflows, `.git*`, paths outside `test_dirs`. It is applied to the index of a
  disposable tree; the result reports its SHA-256, paths, original and effective trees. A run whose overlay test
  files did not execute is `no_tests_executed`, never green.
* Statuses: `queued running passed failed no_tests_collected no_tests_executed setup_error timeout canceled merge_conflict`.
  `passed` requires pytest exit 0, a session-finish event, no failed/errored/incomplete tests, at least one
  executed test, and executed tests for every selector (and every overlay test file).
* `test_result(run_id)` is bound to the submitting principal (OAuth login; `service` for static-token mode;
  `local` for stdio), re-checks the repository allowlist and read access through the GitHub API, and answers
  unknown and foreign run IDs identically.

## Configuration

Git Plus (test execution is **disabled** unless both are set):

| Variable | Meaning |
| --- | --- |
| `GIT_PLUS_TEST_RUNNER_COMMAND` | JSON argv that starts the runner and speaks the frame on stdin/stdout, e.g. `["ssh","-i","/path/key","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","runner@runner-host"]` |
| `GIT_PLUS_TEST_POLICIES` | Maintainer TOML policy (below) |
| `GIT_PLUS_TEST_DATA_DIR` | Results and spool (default `~/.local/state/git-plus/tests`) |
| `GIT_PLUS_TEST_MAX_QUEUED` `..DEFAULT_TIMEOUT` `..MAX_TIMEOUT` | 5, 300 s, 600 s |
| `GIT_PLUS_TEST_RETENTION_HOURS` `..CLOSED_GRACE_HOURS` | 48, 6 |
| `GIT_PLUS_TEST_MAX_SOURCE_MIB` `..MAX_OVERLAY_BYTES` `..MIN_FREE_MIB` | 256, 65536, 1024 |

The runner subprocess started locally inherits only `PATH HOME LANG XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS` and
`GIT_PLUS_RUNNER_*`; GitHub, OAuth and service credentials are never forwarded.

Policy file (repositories not listed cannot be tested; the requirements file must sit beside it):

```toml
[repos."owner/repo"]
profile = "python-pytest"
test_dirs = ["tests"]
requirements = "owner-repo.requirements.txt"   # approved pins only
```

The approved requirements are the only dependency source. They must pin `pytest` and otherwise contain only
`name==version` lines (optional `--hash`, optional environment marker). URLs, paths, editable installs, `-r`,
index options and unpinned requirements are rejected. Contributor manifests are never used; pin the
transitive closure (with hashes, if desired) so rebuilds are reproducible. Packages install with
`pip --only-binary=:all:`, so no sdist build code runs.

Runner (`git-plus-runner`, environment of the runner account):

| Variable | Default |
| --- | --- |
| `GIT_PLUS_RUNNER_IMAGE` | required, `name@sha256:...` or an image ID; must be pre-loaded (`--pull=never`) |
| `GIT_PLUS_RUNNER_ROOT` | `~/.local/state/git-plus-runner` (`envs/`, `jobs/`, `stats.json`) |
| `..CPUS` `..MEMORY_MIB` `..PIDS` | 2, 4096, 512 |
| `..WORK_MIB` `..TMP_MIB` | 1024, 512 (tmpfs; counted against memory) |
| `..MAX_TIMEOUT` `..PREP_TIMEOUT` `..MAX_SOURCE_MIB` | 600 s, 900 s, 256 |
| `..ENV_TTL_DAYS` `..ENV_BUDGET_MIB` `..MIN_FREE_MIB` | 7, 10240, 2048 |
| `..PIP_INDEX_URL` | optional private index/mirror |

## Dependency cache

Key = SHA-256 of (repository, approved requirements digest, Python version, image ID, profile version,
platform). Hit: reused. Miss: built once in a network-enabled preparation container (no source, no
credentials) into `envs/.prep-<key>`, verified (`pytest --version`), marked ready, then published by one atomic
rename. Concurrent requests serialize on a per-key lock; waiters reuse the result (`hit_after_wait`). A keyed
directory without a valid ready marker is never used. Environments are mounted read-only into test jobs, so one
job cannot alter another's dependencies; each job gets its own tmpfs. Changing the requirements, Python,
image, profile version or platform produces a new key. Results report `cache_status`, key, preparation time,
size and cumulative hit/miss counters.

## Cleanup and storage

* Every job's workspace is deleted before the runner replies, and again on SIGTERM/SIGHUP/failure; the
  response states `job_workspace_removed`. Git Plus deletes the spooled artifact after every outcome and on
  restart. Containers are removed by name and have a hard `--timeout`.
* Before each job the runner evicts environments past TTL, then least recently used ones while over budget or
  under the free-space floor, skipping any held by a running job (shared lock) and the one the job needs. If the
  floor cannot be restored the job fails with stage `resources` and a clear message. Crash debris (`.prep-*`,
  `.evict-*`, incomplete environments, workspaces with no live owner) is swept.
* Results are JSON files retained 48 h. When `test_result` observes a closed or merged PR, that PR's results
  expire after the grace period. Approval does not shorten retention. There are no webhooks; cleanup is lazy.
* The result store is in-process state plus files: a restart marks unfinished runs `setup_error`
  (`service_restarted`). Queue capacity is 5; one job runs at a time.

## Runner host setup (prerequisites for production)

1. A dedicated VM/host. Unprivileged account `gp-runner`, rootless Podman with `/etc/subuid` ranges, no sudo,
   no cloud credentials, no GitHub tokens, no mounts of Git Plus data. Egress only to the package index for
   dependency builds; ingress only SSH from hosting-bay; no route to internal services.
2. Install Git Plus (`pip install .`) for the account; load the pinned image by digest
   (`podman pull python@sha256:...`) and set `GIT_PLUS_RUNNER_IMAGE` to it in the account's environment.
3. `authorized_keys`: `restrict,command="/home/gp-runner/.venv/bin/git-plus-runner" ssh-ed25519 AAAA...` for the
   Git Plus key only. Pin the host key in `known_hosts` on hosting-bay.
4. On hosting-bay set `GIT_PLUS_TEST_RUNNER_COMMAND`, `GIT_PLUS_TEST_POLICIES` and `GIT_PLUS_TEST_DATA_DIR`.
   Review and approve every policy requirements file.

Do not run `git-plus-runner` on hosting-bay itself.

## Rollback

Unset `GIT_PLUS_TEST_RUNNER_COMMAND` and restart: both tools then report that test execution is not
configured and nothing else changes. To remove state: delete `GIT_PLUS_TEST_DATA_DIR` and the runner's
`GIT_PLUS_RUNNER_ROOT`. To roll back code, redeploy the previous commit (`4eab036` precedes this feature).

## Validation status

Unit and Git-fixture tests cover validation, revisions, overlays, classification, queueing and cache logic.
`tests/test_sandbox_integration.py` runs the real runner in real rootless Podman and is skipped unless
`GIT_PLUS_TEST_RUNNER_IMAGE` names a local image. Not exercised: the SSH transport against a real remote
runner host, Docker as the engine, resource limits under sustained load, and sandbox-escape resistance beyond
the boundary assertions in those tests.
