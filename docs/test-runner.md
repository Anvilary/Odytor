# Revision-pinned test runs (`test_run`, `test_result`)

Odytor can run a repository's pytest suite against an exact PR revision, optionally with a
reviewer-written test overlay, and return structured evidence. Nothing is committed, pushed or posted.

## Architecture and trust boundary

```text
MCP caller -> Odytor (trusted, holds GitHub credentials)
                 validate caller, repo, PR, exact head/base, selectors, overlay
                 fetch, optional merge-tree, apply overlay to an index, export tree.tar (no credentials)
                 bounded queue (1 worker) ---stdin: header + tree.tar---> odytor-runner (separate host)
                                                                          validates the job again
                                                                          reuses/builds read-only dependency env
                                                                          rootless container: no network, no credentials
                 <---stdout: bounded JSON evidence---------------------- deletes workspace
```

* **Odytor never executes repository code.** Execution happens only in `odytor-runner`, which must run
  on a dedicated runner host or VM with no GitHub tokens, no Odytor data and no route to production services.
* The runner accepts one thing: a framed job (profile `python-pytest`, validated selectors, approved
  requirements, a tar artifact). It has no shell, no command endpoint and no network listener. In production
  it is reached over SSH with a key restricted to a forced command, from a root-owned `authorized_keys` the
  runner account cannot edit.
* **Connection lifetime is the job lifetime.** The client keeps the runner's stdin open until it has read the
  answer. EOF on stdin therefore means the client is gone (killed process, lost SSH session, cancellation): the
  runner stops the container, removes the workspace and exits. The runner keeps no results; a dropped
  connection is reported by Odytor as `setup_error` (stage `runner`) and the run can be resubmitted. A frame
  that does not arrive within `ODYTOR_RUNNER_FRAME_TIMEOUT` (600 s) is abandoned, and a second submission
  of a job id that is still running is refused (`duplicate`) without touching the first.
* `odytor-runner` is the only code that starts containers. Test containers run with `--network=none`,
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

Odytor (test execution is **disabled** unless both are set):

| Variable | Meaning |
| --- | --- |
| `ODYTOR_TEST_RUNNER_COMMAND` | JSON argv that starts the runner and speaks the frame on stdin/stdout (see the SSH command below) |
| `ODYTOR_TEST_POLICIES` | Maintainer TOML policy (below) |
| `ODYTOR_TEST_DATA_DIR` | Results and spool (default `~/.local/state/odytor/tests`; an existing `~/.local/state/git-plus/tests` keeps being used) |
| `ODYTOR_TEST_MAX_QUEUED` `..DEFAULT_TIMEOUT` `..MAX_TIMEOUT` | 5, 300 s, 600 s |
| `ODYTOR_TEST_RETENTION_HOURS` `..CLOSED_GRACE_HOURS` | 48, 6 |
| `ODYTOR_TEST_MAX_SOURCE_MIB` `..MAX_OVERLAY_BYTES` `..MIN_FREE_MIB` | 256, 65536, 1024 |

Recommended SSH command (no secrets on the command line: the key is a file, the host key is pinned in a file the
runner command names, nothing is forwarded, a half-open connection is dropped within about 45 s):

```json
["ssh","-T","-i","/etc/odytor/runner_key","-o","IdentitiesOnly=yes","-o","BatchMode=yes",
 "-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile=/etc/odytor/runner_known_hosts",
 "-o","ConnectTimeout=10","-o","ServerAliveInterval=15","-o","ServerAliveCountMax=3",
 "-o","ClearAllForwardings=yes","-o","ForwardAgent=no","gp-runner@runner-host"]
```

`-T` matters: a pseudo-terminal would corrupt the binary frame. `StrictHostKeyChecking=yes` with a pinned
`known_hosts` makes a changed or unknown host key a hard failure before any job data is sent.

The runner subprocess started locally inherits only `PATH HOME LANG XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS` and
`ODYTOR_RUNNER_*`; GitHub, OAuth and service credentials are never forwarded.

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

Runner (`odytor-runner`, environment of the runner account):

| Variable | Default |
| --- | --- |
| `ODYTOR_RUNNER_IMAGE` | required, `name@sha256:...` or an image ID; must be pre-loaded (`--pull=never`) |
| `ODYTOR_RUNNER_ROOT` | `~/.local/state/odytor-runner` (`envs/`, `jobs/`, `stats.json`); an existing `~/.local/state/git-plus-runner` keeps being used, so a renamed runner keeps its dependency cache |
| `..CPUS` `..MEMORY_MIB` `..PIDS` | 2, 4096, 512 |
| `..WORK_MIB` `..TMP_MIB` | 1024, 512 (tmpfs; counted against memory) |
| `..MAX_TIMEOUT` `..PREP_TIMEOUT` `..MAX_SOURCE_MIB` `..FRAME_TIMEOUT` | 600 s, 900 s, 256, 600 s |
| `ODYTOR_RUNNER_MAX_QUEUED` `..QUEUE_WAIT` | 5, 900 s: host-wide admission (below) |
| `ODYTOR_RUNNER_CONFIG` | optional file of `ODYTOR_RUNNER_*=value` lines; must be root-owned and not group/world writable (the provisioning script uses `/etc/git-plus-runner/runner.env`) |
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

### Approved dependency policies (`deploy/policies/`)

`deploy/policies/` contains example maintainer policy configuration (`policy.toml`) and approved requirement files (`example-webapp.requirements.txt`, `smoke-test.requirements.txt`). They are the only dependency source for `python-pytest`: a PR cannot add, change, or remove dependencies through its own manifest files. For production use, generate a full transitive closure pinned with `name==version` and `--hash=sha256:...` for your target runner platform so pip enforces hash-checking mode with `--only-binary=:all:`. To update a repository's approved dependencies, regenerate its requirements file from a reviewed base branch, inspect the diff, and replace it; the new digest produces a new cache key and the old environment ages out.

## Cleanup and storage

* Every job's workspace is deleted before the runner replies, and again on SIGTERM/SIGHUP/failure; the
  response states `job_workspace_removed`. Odytor deletes the spooled artifact after every outcome and on
  restart. Containers are removed by name and have a hard `--timeout`.
* Before each job the runner evicts environments past TTL, then least recently used ones while over budget or
  under the free-space floor, skipping any held by a running job (shared lock) and the one the job needs. If the
  floor cannot be restored the job fails with stage `resources` and a clear message. Crash debris (`.prep-*`,
  `.evict-*`, incomplete environments, workspaces with no live owner) is swept.
* Abnormal termination. Client disconnect, SIGTERM and SIGHUP unwind through `finally` blocks that remove the
  named container and the workspace. If the runner process itself is SIGKILLed nothing can clean up, and the
  container keeps running until its hard `--timeout` (job timeout + 60 s) because Podman's conmon outlives its
  client. Every container is labelled with its runner root and workspace; before each job and in the 6-hourly
  `odytor-runner gc` timer, containers whose workspace lock is no longer held are removed and the dead
  workspace is deleted. A running job always holds its lock, so housekeeping never touches it. After a host
  reboot there are no containers; leftover workspaces are swept the same way and the dependency cache is intact.
* Results are JSON files retained 48 h. When `test_result` observes a closed or merged PR, that PR's results
  expire after the grace period. Approval does not shorten retention. There are no webhooks; cleanup is lazy.
* The result store is in-process state plus files. Several local agents (stdio processes) may share one
  `ODYTOR_TEST_DATA_DIR`: each process holds a liveness lock for its lifetime, results are readable by every
  process of the same principal, and a starting process closes (`setup_error`, `service_restarted`) only the
  runs of instances that are really gone, never those of a live sibling. Queue capacity is 5 per process; the
  runner serializes dependency preparation per cache key, so concurrent agents never build the same
  environment twice.

## Runner host setup (production)

The runner must be a dedicated VM (or host) that holds no GitHub tokens, no Odytor data and has no route to
production services. Do not run `odytor-runner` on the Odytor host. `deploy/runner/provision.sh` builds the
whole host on Debian 13 and is idempotent; it was developed against and re-run on a real KVM VM.

```text
sudo env GP_RUNNER_IMAGE=docker.io/library/python@sha256:<digest> \
         GP_AUTHORIZED_KEY_FILE=/root/git-plus-runner.pub \
         GP_WHEEL=/root/odytor-0.1.0-py3-none-any.whl \
         GP_ALLOW_FROM=<address of the Odytor host> \
         bash deploy/runner/provision.sh
```

What it installs and why:

| Item | Detail |
| --- | --- |
| Account | `gp-runner`: password locked, no sudo, subordinate UID/GID range 200000-265535, lingering user manager so rootless Podman works from an SSH session. |
| Limits | `Delegate=cpu cpuset io memory pids` for `user@.service`, without which rootless `--cpus/--memory/--pids-limit` are not enforceable. `git-plus-runner check` fails if they are missing. |
| Software | A venv under `/opt/git-plus-runner` installed `--no-deps` (the runner is standard-library only), root-owned; the forced-command script in `/usr/local/libexec`, root-owned; config in `/etc/git-plus-runner/runner.env`, root-owned. The runner account can change none of them. |
| SSH | A `Match User gp-runner` block: key-only, `ForceCommand`, no TTY, no TCP/stream-local/agent/X11 forwarding, no tunnels, no rc files, `ClientAliveInterval 15` x 3 so a vanished client is noticed. `AuthorizedKeysFile` is `/etc/ssh/git-plus-runner.authorized_keys` (root-owned) with `restrict,command="...",from="<GP_ALLOW_FROM>"`. |
| Firewall | nftables: input drop except SSH from `GP_ALLOW_FROM`; the account's egress (dependency preparation only) allows DNS and the public internet but drops private, link-local, loopback and carrier-grade NAT ranges. Test containers have no network at all. |
| Image | Pulled by digest as `gp-runner`; the runner refuses to start a job if the local image does not match the pinned digest or ID. |
| Housekeeping | `git-plus-runner-gc.timer` (boot + 6 hourly) runs `odytor-runner gc`: orphan containers, dead workspaces, TTL/budget eviction. |
| Validation | `odytor-runner check` is run at the end: rootless, cgroup limits delegated, free space, image, and an isolated container (user 65534, no network) actually runs. |

The forced command ignores whatever the client asks to run (it is only visible to the script as
`SSH_ORIGINAL_COMMAND`, which is never read). `odytor-runner check|gc` are reachable only from a local
administrator shell or the timer.

On the Odytor host (not the runner): create a dedicated key (`ssh-keygen -t ed25519 -f runner_key`), give the
`.pub` to the provisioning script, put the runner's host public key in a `known_hosts` file, set
`ODYTOR_TEST_RUNNER_COMMAND` as above, `ODYTOR_TEST_POLICIES` and `ODYTOR_TEST_DATA_DIR`, and review every
policy requirements file. Do not put the SSH key material in the command line or logs; the command names files.

Dependency preparation (the only step with network) runs `pip --only-binary=:all:` for pins from the maintainer
policy, never from a contributor branch. It runs as container root, which is the unprivileged `gp-runner` user
through the user namespace, so the environment files belong to the runner account and are mounted read-only
into test jobs. Prefer hash-pinned requirements and a package mirror (`ODYTOR_RUNNER_PIP_INDEX_URL`) in production.

### Host-wide admission

One job executes at a time on a runner, however many Odytor processes or hosts connect. Each job takes a
ticket (a file it holds a lock on while alive) and runs only when its ticket is the oldest live one, so jobs are
served in arrival order and a crashed or disconnected job releases its place by itself. At most
`ODYTOR_RUNNER_MAX_QUEUED` jobs wait; beyond that a submission is refused immediately with `setup_error` stage
`busy` and nothing runs. A job that waits longer than `ODYTOR_RUNNER_QUEUE_WAIT` is also refused as `busy`.
The time spent waiting is reported as `execution.runner_queue_wait_seconds`. Per-job CPU, memory and pid limits
still apply inside the single slot.

### Sizing and re-provisioning

Size the VM for one job at a time (the job memory limit plus the guest system, and the environment cache). `provision.sh` restarts
the account's user manager (needed for cgroup delegation), which stops Podman and any job in flight: drain the
runner (no running jobs) before re-provisioning an in-use host.

### Residual risk

Container isolation does not protect against kernel or container-runtime vulnerabilities. The VM boundary is the
real control: keep the runner single-purpose, patched and without credentials, keep SSH reachable only from the
Odytor host, and rebuild it from `provision.sh` if it is ever suspected. Test code can read everything inside
its container (the source and the dependencies), and can forge pytest output, which the classifier cross-checks
but cannot prove (see Result integrity). A SIGKILLed runner leaves its container running for at most the job
timeout + 60 s.

## Rollback

* Test feature only: unset `ODYTOR_TEST_RUNNER_COMMAND` and restart. Both tools then answer that test
  execution is not configured; nothing else changes (OAuth storage, the classic PAT and the other six tools are
  untouched). The new tools are registered unconditionally and become inert, so no separate un-registration is
  needed.
* Application image: redeploy a previous release image. Persistent OAuth
  storage is a separate volume; never run `docker compose down -v`.
* Runner configuration: restore the previous `/etc/git-plus-runner/runner.env` and re-run `provision.sh` with the
  previous image digest. To disable the runner without deleting it, remove `/etc/ssh/git-plus-runner.authorized_keys`
  (sshd then refuses the account) or stop sshd.
* Runner removal: delete the VM. To clear state only, delete `ODYTOR_TEST_DATA_DIR` on the Odytor host and
  `~gp-runner/.local/state/git-plus-runner` on the runner.

## Validation status

Real, not mocked (see `tests/test_sandbox_integration.py` and `tests/test_ssh_runner.py`): the runner in rootless
Podman with the real classifier; a real OpenSSH client against a real sshd in a KVM VM provisioned by
`deploy/runner/provision.sh`, covering the forced command, protocol abuse, oversized and interrupted frames,
disconnects, duplicates, SIGKILL, host-key mismatch, hostile servers, resource limits, cache reuse, invalidation,
corruption, concurrency and disk pressure. The SSH tests are skipped unless `ODYTOR_TEST_SSH_RUNNER_DIR` names
a provisioned runner. Unit and Git-fixture tests cover validation, revisions, overlays, classification, queueing
and cache logic. Not exercised: Docker as the engine, a production runner VM, the production OAuth flow, and
sandbox-escape resistance beyond the boundary assertions in those tests.

## Execution profiles, discovery and artifacts (remote engineering workspace)

The runner serves more than pytest. Profiles are defined in trusted code (`odytor/profile.py`, shared by Odytor and the
runner) and enabled per repository by the maintainer policy; **repository files cannot add or widen a profile**.

| Profile | Runtime | Evidence rule |
|---|---|---|
| `python-pytest` | Python 3.12 with maintainer-approved pinned requirements (the only profile whose preparation container has network) | Unchanged: executed passing tests, selector coverage, exit status and report consistency. |
| `node-test` | `node --test` with the TAP reporter, no dependency installation | Only the runner's own TAP summary counts. A test file that merely loads is **not** a passing test (Node counts it as one; it is excluded), so a run with no real tests is `no_tests_collected`. |
| `browser-playwright` | Chromium via Playwright against the tree served inside the container | Green needs every step ok, a page loaded and at least one assertion; see [browser](browser.md). |

All profiles run with no network, a read-only root, no capabilities, user `65534`, pinned image, and the configured CPU,
memory, PID and tmpfs limits. A job header is validated again by the runner against the registry; unknown profiles,
versions, selectors, scenarios or serve settings are `validation` setup errors.

### Protocol additions (schema stays 1)

* **Probe frame.** A frame whose header is `{"schema":1,"op":"probe"}` and whose artifact length is 0 makes the runner
  report observed facts (engine, rootless, cgroup controllers, image id and tools, per-profile `installed`, limits,
  storage) without starting a job container. `odytor_capabilities(probe_runner=true)` uses it. An older runner rejects it
  as a validation error, which is reported as `probe_unsupported`.
* **Artifacts.** Profiles that produce them return base64 items in their report; the report ceiling for such jobs is
  40 MiB (4 MiB otherwise). The control plane re-derives type, size and hash and applies quotas ([artifacts](artifacts.md)).
* **Image tools.** The runner observes what the image provides (Python, Node, Playwright, Chromium) once per image and
  caches it; a profile whose tools are missing fails fast as `profile_unavailable`.

### Runner image

`deploy/runner/image/Containerfile` builds a Python + Node 22 + Chromium (Playwright 1.63) image from digest-pinned
bases. Build it, load it on the runner host and pin the result by digest in `ODYTOR_RUNNER_IMAGE`. The existing
Python-only image keeps working for `python-pytest`; `node-test` and `browser-playwright` are then reported
`installed: false`. Refresh the pinned bases deliberately and re-run the integration tests.

### Cancellation

`test_cancel` marks a run canceled. Odytor drops the runner connection; the runner sees EOF on stdin, stops the
container (and with it every descendant, including Chromium) and removes its job workspace. Verified with real
containers for both pytest and browser runs.
