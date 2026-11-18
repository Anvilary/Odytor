# Private deployment on hosting-bay: operations, rollback and dogfooding

Eight tools run from one container; `test_run` / `test_result` execute untrusted PR code only in a dedicated KVM
guest. This is the private-testing deployment, not a public release.

## Layout

```text
Cloudflare tunnel -> git-plus (container, edge + gp-runner-net)  -- SSH, forced command -->  git-plus-runner-vm
                                                                    (QEMU/KVM guest, 2 vCPU, 3 GiB, 40 GiB thin)
                                                                         rootless Podman -> disposable test containers
```

| Piece | Where |
| --- | --- |
| Application | container `git-plus`, image `git-plus:<commit>`, `~/services/git-plus/compose.yaml`, `.env` (OAuth, allowlists, PAT: unchanged) |
| Runner key and pinned host key | `~/services/git-plus/runner-ssh/` (`runner_key` 0640 for the container's group, `known_hosts`) |
| Approved dependency policy | `~/services/git-plus/policies/` (read-only mount); source of truth `deploy/policies/` |
| Results and spool | named volume `git_plus_test_data` (separate from the OAuth volume `git_plus_data`) |
| VM (QEMU in an unprivileged container, no host root) | container `git-plus-runner-vm`, network `gp-runner-net` 172.30.77.0/28, disk and keys in `~/services/git-plus-runner-vm/` (0700) |
| Scripts | `~/services/git-plus-runner/hosting-bay/runner-vm.sh` (`status`, `ssh`, `stop`, `start`, `provision`) |

Why QEMU in a container: hosting-bay has no passwordless `sudo`, so no packages could be installed. The container
runs as the unprivileged host account with no capabilities, `no-new-privileges`, a read-only root filesystem,
`/dev/kvm` as its only device, 3.5 GiB / 2 CPUs / 256 pids limits and no published ports. The guest has its own
kernel; that is the isolation boundary for untrusted code. SSH to the guest is reachable only from members of
`gp-runner-net` (the application, and the host through the bridge gateway). The guest's firewall and the key's
`from=` accept only that subnet; the runner account may reach DNS and the public package index but no private,
link-local, CGNAT or loopback range.

Resource budget on the 11.9 GiB host (measured 2026-11-18, memory pressure PSI 0 before and after): the guest uses
about 0.45 GiB idle and 0.7 GiB after jobs (virtio balloon with free-page reporting returns unused guest memory),
hard-capped at 3.5 GiB by the container. One job at a time, at most 1.75 GiB of it (`GIT_PLUS_RUNNER_MEMORY_MIB`
1787, derived from guest RAM; `git-plus-runner check` refuses a limit above 75% of guest RAM).

## Everyday operations

```text
ssh hosting-bay
cd ~/services/git-plus-runner/hosting-bay
./runner-vm.sh status                      # container state and VM resource use
./runner-vm.sh ssh 'sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry check'   # needs `cd /` first
docker logs --since 1h git-plus            # application log (no secrets are logged)
```

Housekeeping needs no action: orphan containers, dead workspaces and expired or over-budget environments are
removed before every job and by a 6-hourly timer. Environment TTL 7 days, budget 10 GiB, minimum free 2 GiB.
Retained results: 48 h, expiring 6 h after a PR is seen closed or merged.

## Deploying a new application commit

1. `git archive` the tested commit, transfer, `docker build -f deploy/app/Dockerfile` to `git-plus:<commit>`.
2. Copy `compose.yaml` to `compose.yaml.before-<commit>`, change only the `image:` tag.
3. `docker compose up -d --no-deps git-plus` (never `down -v`; the tunnel and Nginx are not touched), wait for `healthy`.
4. Check `https://git-plus.alexandreteixeira.dev/.well-known/oauth-authorization-server` (200) and an unauthenticated
   `POST /mcp` (401 with `WWW-Authenticate`).

Updating the runner software: build the wheel, `GP_RUNNER_IMAGE=... GP_WHEEL=... ./runner-vm.sh provision`. It is
idempotent and does not restart the user manager unless cgroup delegation changed. Drain jobs first anyway.
Changing a dependency policy: replace the file in `policies/` (a new digest builds a new environment; the old one
ages out); no restart is needed because policies are read at startup, so restart the application container.

## Rollback

* Test tools only: remove the `GIT_PLUS_TEST_*` lines from `compose.yaml` (or stop the VM). Both tools then report
  that test execution is not configured or unavailable; the other six tools and OAuth are unaffected.
* Application: `cp compose.yaml.before-<commit> compose.yaml && docker compose up -d --no-deps git-plus`. The previous
  images (`git-plus:4eab036` and earlier) are kept. The OAuth volume is never touched.
* VM: `./runner-vm.sh stop` (graceful guest poweroff). To discard it: `docker rm -f git-plus-runner-vm`, delete
  `~/services/git-plus-runner-vm/disk/vm.qcow2`, then `init` and `provision` rebuild it in about three minutes.
  Remove the network with `docker network rm gp-runner-net` once nothing is attached.

## Known limitations

* Authentication: the hosted OAuth flow cannot be completed non-interactively; it was verified by metadata,
  challenge and rejection checks, and by running the same image's stdio server inside the container with the
  server-held PAT. The first real hosted `test_run` from ChatGPT or Perplexity is the remaining user-side check.
* One job at a time; five may wait. A sixth submission is refused as `busy`, a waiter gives up after 15 minutes.
* The VM's SSH source check sees the Docker network, not individual containers: any container attached to
  `gp-runner-net` can try the port (the key is still required). Only `git-plus` is attached.
* Container isolation is not proof against kernel or QEMU vulnerabilities; an escape would land as an unprivileged
  host account in a container with no capabilities. Keep the guest patched; rebuild it from scripts when in doubt.
* A host reboot powers the guest off abruptly (Docker stops the container); it restarts with Docker and recovers
  its journal and cache. No runs survive a restart; they are reported as `setup_error`.
* Only Python/pytest, only repositories listed in `policy.toml` (currently `odysseus-dev/odysseus`).
* Guest and application containers are not backed up: the cache is rebuildable and results are disposable.

## Dogfooding checklist (read-only unless stated; do not mutate real Odysseus PRs to complete it)

Setup: connect the MCP in ChatGPT/Perplexity (OAuth as the allowed user). PR #6161 is the reference.

1. `pr_evidence` on a PR: head/base revisions, files, review and check state agree with GitHub.
2. `ci_diagnose` on a PR with a failing check: failed jobs classified; CodeQL alerts listed or an explicit error,
   never silently empty.
3. `pr_conflicts` on a clean PR and on a conflicting one (#7300 at the time of writing): paths listed.
4. `test_run` head mode with selectors `tests/test_require_admin_loopback_scope.py` and
   `tests/test_internal_token_middleware_asgi.py`; then `test_result` until terminal. Expect 38 passed, `green: true`,
   first run `cache_status: miss` (about 35 s), later runs `hit`.
5. Same with a reviewer `test_overlay` (a small new test file under `tests/`): the result shows the overlay digest,
   original and effective trees, and the overlay test executed. Ask for a deliberately failing overlay: `failed`.
6. `test_run` with `mode=prospective_merge` and `expected_base` equal to the live `dev` tip: merge tree recorded;
   with a stale base or head: rejected with the actual revision; on a conflicting PR: `merge_conflict`, nothing ran.
7. Ask for an empty selection, an all-skipped file or a hanging test: `setup_error` / `no_tests_executed` /
   `timeout`, never green.
8. Authentication: use the tools from a GitHub account that is not in `GIT_PLUS_ALLOWED_GITHUB_USERS` (expect a
   refusal), and ask for a repository outside `GIT_PLUS_ALLOWED_REPOSITORIES` (refusal).
9. Write tools (`quick_fix`, `pr_rebase`, `ci_control`): exercise only on a throwaway branch/PR in a repository you own
   and have added to the allowlist; they are unchanged by this deployment. Contributor-fork edits additionally need
   a fork PR with maintainer edits allowed.
10. Resource and cleanup: after a session run `./runner-vm.sh ssh 'cd /; sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry gc'`
    and `./runner-vm.sh status`; expect no containers, no job workspaces, VM memory well under 1 GiB, host
    `/proc/pressure/memory` near zero.
11. Two clients at once (for example ChatGPT and a local agent): the second run reports `runner_queue_wait_seconds`
    greater than zero and both finish.
