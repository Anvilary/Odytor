# Private deployment on hosting-bay: operations, rollback and dogfooding

Eight tools run from one container; `test_run` / `test_result` execute untrusted PR code only in a dedicated KVM
guest. This is the private-testing deployment, not a public release.

## Layout

```text
Cloudflare tunnel -> app (container "git-plus", edge + gp-runner-net)  -- SSH, forced command -->  git-plus-runner-vm
                                                                    (QEMU/KVM guest, 2 vCPU, 3 GiB, 40 GiB thin)
                                                                         rootless Podman -> disposable test containers
```

| Piece | Where |
| --- | --- |
| Application | container `git-plus` (legacy Compose container name; Nginx and the tunnel address it), image `odytor:<commit>` (current: `odytor:cb446ef`), `~/services/git-plus/compose.yaml`, `.env` (canonical URL `https://odytor.alexandreteixeira.dev`) |
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
hard-capped at 3.5 GiB by the container. One job at a time, at most 1.75 GiB of it (`ODYTOR_RUNNER_MEMORY_MIB`
1787, derived from guest RAM; `odytor-runner check` refuses a limit above 75% of guest RAM).

## Everyday operations

```text
ssh hosting-bay
cd ~/services/git-plus-runner/hosting-bay
./runner-vm.sh status                      # container state and VM resource use
./runner-vm.sh ssh 'sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry check'   # needs `cd /` first
docker logs --since 1h git-plus            # application log (no secrets are logged)
```

Runtime housekeeping removes orphan containers, dead workspaces and expired or over-budget environments before
every job and by a 6-hourly timer. Environment TTL 7 days, budget 10 GiB, minimum free 2 GiB. Retained results:
48 h, expiring 6 h after a PR is seen closed or merged. This runtime cleanup does not manage host image tags,
runner images, wheel files, VM disks or build caches. Manage those separately with the scoped procedure below.

## Deploying a new application commit

1. Record the source commit and deployment time. Build the app image from that exact tested source; use a commit
   tag and record the image ID/digest. Build a runner wheel from that same source revision and record its SHA-256.
   The runner image must also be pinned by digest. Keep these provenance records with the release evidence.
2. Copy `compose.yaml` to `compose.yaml.before-<new-commit>`, preserving it as the rollback file. Change only
   the application image tag. Before deploying, confirm the currently running image/tag and that the previous
   compose file and image are present.
3. Drain admission before replacing the app. On a revision that supports it, run `odytor --drain start` and
   poll `odytor --drain status` until it reports both `draining` and `quiescent`. Do not interpret `draining`
   alone as quiescence: accepted campaigns and reservations retain their shared admission lease and can finish.
   Keep the persistent run-data volume mounted throughout the upgrade. Resume admission with
   `odytor --drain resume` after verification or rollback.
4. Revision `2511b7e` predates the drain command. For deployments from that revision, close admission at the
   ingress/tool boundary before stopping the old process. The gate must reject new MCP tool calls while allowing
   already accepted work to finish; verify this with an operator-observable request log or equivalent evidence,
   then wait for in-flight calls and reservations to reach zero. A maintenance page alone is insufficient if
   `/mcp` remains reachable. If no such gate and evidence are available, do not replace the running process.
5. Deploy with `docker compose up -d --no-deps git-plus` (never `down -v`; the tunnel and Nginx are not touched),
   wait for `healthy`, and verify `https://odytor.alexandreteixeira.dev/.well-known/oauth-authorization-server`
   (200) and an unauthenticated `POST /mcp` (401 with `WWW-Authenticate`). Confirm the reported build revision
   through `odytor_capabilities` where supported. Perform read-only preparation checks first. Do not exercise
   GitHub write tools on real PRs; separately configure allowed groups for preparation and retain all existing
   GitHub write prompts and authorization checks.
6. Resume admission, then verify a fresh read-only request and inspect application logs. Save the deployed revision,
   image digest, wheel hash, health checks, drain evidence and any verification output with the release record.

Updating the runner software: build the wheel from the application release source and verify its SHA-256 against
the recorded value. Pin `GP_RUNNER_IMAGE` to the runner image digest built for the same release, then run
`GP_RUNNER_IMAGE=... GP_WHEEL=... ./runner-vm.sh provision`. Record the guest's installed runner version and
image digest after provisioning. Drain application admission and wait for quiescence before provisioning. The
provision command is idempotent and does not restart the user manager unless cgroup delegation changed.
Publish approved requirements before atomically replacing `policy.toml` in the mounted policy directory. The
service loads policy at admission. An invalid policy rejects new execution and reports a diagnostic in
`odytor_capabilities`. An admitted job retains its original policy and deadline.

## Rollback

* Test tools only: remove the `ODYTOR_TEST_*` lines from `compose.yaml` (or stop the VM). Both tools then report
  that test execution is not configured or unavailable; the other six tools and OAuth are unaffected.
* Application: reapply the matching saved compose file and image, then `docker compose up -d --no-deps git-plus`.
  Confirm health and OAuth metadata/challenge checks before resuming admission. The previous images and runner
  wheel/image must remain available until the rollback window closes. The OAuth volume and persistent run-data
  volume are never touched.
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
* Available test profiles depend on the pinned image and each repository's entry in `policy.toml`.
* Guest and application containers are not backed up: the cache is rebuildable and results are disposable.

## Scoped host and runner retention and headroom

Use `deploy/hosting-bay/retention-headroom.sh` independently on each storage owner, once on the host and once in
the runner guest (or through the approved administrative SSH session). It reports free MiB for supplied roots and
prints candidates by default. It does not call Docker or Podman cleanup and never clears build caches. Candidate
and keep manifests are newline-separated absolute paths. Include only exact, reviewed app/runner image archive
files, obsolete wheel files, or other deployment artifacts under the declared roots. Keep manifests must include
the active deployment, the complete known rollback set, and any artifact used by an in-progress release. Shared
or ambiguous caches stay untouched. Directories require especially careful inventory because a directory candidate
is removed recursively.

Example, after reviewing both manifests and confirming paths against the deployment inventory:

```sh
./retention-headroom.sh --root "$HOME/services/git-plus/releases" \
  --root "$HOME/services/git-plus-runner/releases" \
  --candidates /secure/reviewed-candidates.txt --keep /secure/keep-active-and-rollback.txt \
  --min-free-mib 2048
# Inspect the proposed removals and headroom report. Repeat with --apply only for reviewed candidates.
```

Run the command separately on each host or guest so its `df` values describe the filesystem where the artifacts
live. The example minimum is 2 GiB free, matching the runner environment's existing minimum; choose a larger
threshold when the next image build, VM growth or backup needs it. The script exits with status 1 below the
threshold. Reclaim reviewed candidates or expand storage. Refresh candidate and keep manifests
for every release. Never list active/in-use resources, rollback images, the VM disk, mounted persistent data,
container storage internals, or shared build-cache directories as candidates.

Keep the running release and at least one boot-tested rollback release for both the application and the runner.
Keep their compose files, exact image IDs and tags, checked wheels, and policy snapshots together. Inventory
named deployments with `docker image ls` on hosting-bay and `podman image ls` as the runner user. Before removing
each reviewed obsolete tag, resolve its image ID and check all containers, including stopped containers, with
`docker ps -a --filter ancestor=<image-id>` or `podman ps -a --filter ancestor=<image-id>`. Skip references
from any container or rollback manifest. Remove only the exact tag with `docker image rm --no-prune <tag>` or
`podman image rm --no-prune <tag>` without force. Measure filesystem headroom again because image sizes include
shared layers. Leave shared build caches in place when their owner cannot be determined. Use per-release archive
and build-context directories for future builds so those paths can be retired explicitly.

Before any planned host or guest maintenance, capture a recoverable backup of the release metadata and manifests,
compose and rollback compose files, policy/configuration files without secrets, and persistent run-data volume if
it contains evidence needed for recovery. Store credentials through the existing secret process, never in the
backup bundle. Record the backup location, completion time and checksum, and test that the archive can be listed.
The runner VM disk is not an application backup; preserve it unless a separate tested rebuild/recovery plan exists.

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
8. Authentication: use the tools from a GitHub account that is not in `ODYTOR_ALLOWED_GITHUB_USERS` (expect a
   refusal), and ask for a repository outside `ODYTOR_ALLOWED_REPOSITORIES` (refusal).
9. Write tools (`quick_fix`, `pr_rebase`, `ci_control`): exercise only on a throwaway branch/PR in a repository you own
   and have added to the allowlist; they are unchanged by this deployment. Contributor-fork edits additionally need
   a fork PR with maintainer edits allowed.
10. Resource and cleanup: after a session run `./runner-vm.sh ssh 'cd /; sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry gc'`
    and `./runner-vm.sh status`; expect no containers, no job workspaces, VM memory well under 1 GiB, host
    `/proc/pressure/memory` near zero.
11. Two clients at once (for example ChatGPT and a local agent): the second run reports `runner_queue_wait_seconds`
    greater than zero and both finish.
