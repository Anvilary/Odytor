# Remote engineering workspace: engineering report

Branch `feat/remote-engineering-workspace` (local; nothing pushed, nothing deployed). Base `a92c4c6`. Status words are
exact: **PASS**, **FAIL**, **SKIPPED** (the test ran nowhere because its prerequisite was absent), **NOT TESTED**
(no test exists or no access).

## A. Implementation

**Revisions.** Source: the branch head (see section E). The previously deployed revision `a92c4c69871f40c9086acea22ec6c9a17d8dee33`
is the branch base. **The production revision was not verified**: this checkout cannot observe the deployed instance;
`odytor_capabilities` reports the running revision once the image is stamped with `ODYTOR_BUILD_REVISION`.

**Modules added:** `workspace`, `artifacts`, `browser`, `browser_spec`, `browser_driver`, `publish`, `capabilities`.
**Modules changed:** `overlay`, `pr_conflicts`, `git_workspace`, `auth`, `config`, `jobs`, `profile`, `harness`, `sandbox`,
`run_tests`, `github`, `server`. `deploy/runner/image/Containerfile` is new.

**MCP tools added (13):** `odytor_capabilities`, `workspace_create`, `workspace_read`, `workspace_search`,
`workspace_edit`, `workspace_diff`, `workspace_run`, `workspace_manage`, `test_cancel`, `browser_run`, `browser_close`,
`artifact_get`, `publish_plan`, `publish_apply`. **Extended (backward compatible):** `pr_conflicts(merge_target)`;
`test_run`/`quick_fix`-style overlay and patch failures now carry `diagnostic.stage`, `line`, a bounded excerpt and
declared-versus-found hunk counts. The original eight tools keep their names, schemas and behavior (asserted by tests).

**Execution profiles:** `python-pytest` (unchanged), `node-test`, `browser-playwright`. **Not implemented:** Python lint
and type check, Node lint/type check/build and npm dependency installation, database migration tests, container-build
validation.

**Workspaces:** create at an exact PR head, prospective merge against a live tip, or exact branch revision; tree, bounded
file ranges, search, symbols; versioned `write`/`replace`/`delete`/`rename`/`patch`/`checkpoint`/`reset`; complete diff
with whole-patch digest; immutable-snapshot test runs; list/status/extend/close; expiry, caps (count, size, files,
checkpoints, runs), restart-safe state. See [workspaces](workspaces.md).

**Browser:** Chromium in the existing sandbox; typed bounded steps incl. assertions, network-request counting and a
race-free `wait_requests`; mocks; console, page-error, network and accessibility evidence; screenshots and traces;
replay-based sessions; no JavaScript evaluation. See [browser](browser.md).

**Artifacts:** owner-bound, sniffed, hashed, quota-bound; images delivered as native MCP image content; chunked base64
fallback; active content only as inert untrusted text. See [artifacts](artifacts.md).

**Authorization changes:** `publish` is a workspace scope operation that is **absent from the default scope**;
`workspace_publish` joined the fork-write operations next to `quick_fix`; the repository allowlist is re-checked on every
workspace call. No approval service was built (see [client compatibility](client-compatibility.md)).

**Publication:** read-only plan bound to tree, patch digest, expected head, validation runs for that tree and
limitations; apply needs `user_authorized=true` plus the plan's confirmation, re-checks live state, applies at most once,
reconciles uncertain outcomes against the remote, never force-pushes. See [publishing](publishing.md).

## B. Tests

Commands (from the repository root, with the virtualenv active):

```text
python -m pytest -q                       # without images: real-sandbox tests are SKIPPED
ODYTOR_TEST_RUNNER_IMAGE=<id of a pinned local python:3.12 image> \
ODYTOR_TEST_BROWSER_IMAGE=<id of localhost/odytor-runner:full-dev, built from deploy/runner/image/Containerfile> \
python -m pytest -q
```

| Run | Result |
|---|---|
| Baseline at `a92c4c6`, no images | 533 passed, 59 skipped |
| Final, both real images | **815 passed**, 0 failed, 33 skipped (all in `test_ssh_runner.py`) |
| Real rootless-container integration (`test_sandbox_integration.py`, 27 tests) | PASS |
| Real workspace edit-test-cancel (`test_workspace_e2e.py`, 4 tests) | PASS |
| Real Chromium + Node (`test_browser_e2e.py`, 10 tests) | PASS |
| Packaging: sdist and wheel build, wheel installs in a fresh venv, `odytor --version`, `odytor-runner` usage, all 32 modules (incl. runner-side) present, no new Python dependency | PASS (manual) |
| Credential scan of the branch diff | PASS (manual): no token-shaped strings, no tracked key or env files |
| `tests/test_ssh_runner.py` (33 tests, needs a provisioned runner VM) | **SKIPPED / NOT TESTED** |
| Any real GitHub write (`publish_apply`, `quick_fix` on a real PR) | **NOT TESTED** (publication is tested against a fake GitHub client and real local git) |
| Remote MCP clients (ChatGPT, Perplexity, Codex, Claude Code UI) | **NOT TESTED** |
| Production runner VM with the new image; `odytor-runner check` on it | **NOT TESTED** |
| CPU limit enforcement | **NOT TESTED** here (the host delegates only memory and pids) |

All 33 skips are in `tests/test_ssh_runner.py` (they need `ODYTOR_TEST_SSH_RUNNER_DIR`, a provisioned runner VM); with both images set nothing else is skipped.

## C. Security

See [security-review.md](security-review.md) for the line-by-line review. In short:

* **Trust boundaries:** control plane (credentials, never executes repository code) → forced-command SSH → runner host
  (no credentials) → rootless container (no network, read-only root, no capabilities, user 65534) → untrusted code.
* **Isolation verified** with real Podman and a real browser: external fetch, cloud-metadata address, other local port and
  external WebSocket are blocked and reported; cancellation removes the container, Chromium and the runner job directory,
  checked at host level with a detector shown to see the processes while they run.
* **Authorization verified:** owner binding on every store, scope narrowing, `publish` absent by default, apply refused
  without authorization, with a wrong confirmation, after workspace change, after head movement, and replays are idempotent.
* **Resource enforcement:** caps verified by test; memory and pids limits observed enforced; **CPU not verified here**.
* **Remaining risks:** container isolation is not proof against kernel or runtime escapes; sandbox evidence can be forged by
  the code under test; app, driver and Chromium share a container, PID namespace and UID; the approval behind
  `user_authorized` is the caller's to obtain; replay sessions assume determinism; production network isolation and the
  deployed endpoint's protection were not inspected.

Defects the tests and the real run caught and fixed: Node counted a test-less file as a pass; the response-safety layer
truncated 64 KiB windows and diffs at 8 KiB; closing a WebSocket in Playwright's route handler deadlocked the driver; a
mock `delay_ms` froze the driver (found on the real PR); one-shot browser runs locked callers out of sessions; the plan
did not warn about failed runs beside a passing one; two error messages were wrong or mangled.

## D. Demonstration (real repository)

Target: `odysseus-dev/odysseus#6479` (open, mergeable-clean, fork `beyremweslati/odysseus` branch `dev`). Head
`862586b063e8eb92fd99588a0191c10e70099f9e`, base snapshot `c21980f5a6d1cb84db8c8ab0f488cefc7764eb3f`, source tree
`aa4513c02ef08c24ff91b3708ffd7567a8c64d65`. Workspace `ws-9d1622a34caa42dbd0c4b4bef17b4a42` (3,047 files). Runner image
built from the Containerfile: image id `922844f6881f…`, Python 3.12.3, Node 22.23.5, Chromium 153, Playwright 1.63.

| §28 step | Where it ran | Result |
|---|---|---|
| 1 workspace at an exact revision | real PR | PASS (version 0, tree `aa4513c…`, source `pr_head`) |
| 2 read relevant files; 3 search surroundings | real PR | PASS (`_renderHwfitFromCache` at line 1394, three call sites, 105 JS symbols) |
| 4 scratch modification | real PR | PASS: added a probe page (v1→v2, scratch only) and reverted the PR's sort fix (v3) |
| 5 add or change tests | fixture e2e only (`test_workspace_e2e.py`) | PASS there; not done on the PR |
| 6 Python validation | real PR | PASS: `test-4f6e856b3da52d9106a7fcb5664c1429`, 7 of 7 executed, approved 109-package environment built in 25 s |
| 7 start the app; 8 drive it with Playwright | real PR | PASS: the PR's real `cookbook-hwfit.js` and its ~60-module graph served and driven in the sandbox, API mocked |
| 9 screenshot | real PR | PASS (10 screenshots across runs, each with the tested tree and a SHA-256) |
| 10 screenshot reaches an MCP client | real PR | PASS through the FastMCP client as native image blocks; **NOT TESTED in any product client UI** |
| 11 inspect the diff | real PR | PASS (restored tree = PR + one added file; regression diff = 1 line added, 5 removed in `cookbook-hwfit.js`) |
| 12 prepare a publication plan | real PR | PASS, but the plan was for **the probe page** `static/odytor-probe-hwfit.html`, not a fix to #6479: it proves the machinery, it is not a proposal for the PR. Plan `pp-f217615d…`, bound to tree `3480f8d54f2c`, patch `88a8db461fc7` |
| 13 no GitHub change without authorization | real PR | PASS: apply refused with a wrong confirmation and without authorization; afterwards the PR is `open`, head still `862586b063e8`, 4 commits, `updated_at` unchanged |
| 14 reset or remove | real PR | PASS (`reset` to the checkpoint reproduced the identical tree; `close` removed the workspace, reads then say unknown) |
| 15 no residual processes | real PR | PASS: 0 `gp-` containers, 0 sandbox browser processes, empty runner job directory |

**Behavior checked on the PR's frontend** (browser runs, mocked API): on load, one `GET /api/hwfit/models`; changing sort:
no request; changing the engine filter: no request; changing the quant filter: a request; Rescan: a request; sorting before
the first response lands: a second request is issued and the late first response does not overwrite the newer list.
All 5 assertions in the main scenario passed on the clean tree (tree `3480f8d54f2c`). With the fix reverted (tree
`afadacf43456`) the same scenario **failed at the "no refetch on sort" assertion** with a failure screenshot (observed 1
request, expected 0); after `reset` the clean tree passed again. **Not exercised:** header-click sorting consistency,
bounded result size, and a delayed response arriving after a newer one (the mock engine applies one delay per path). The
probe page replaces the real application shell, so this is evidence about the module's behavior, not about the full app.

## E. Git status

Branch `feat/remote-engineering-workspace`, working tree clean after the report commit, 7 commits above `a92c4c6`
(`git log --oneline a92c4c6..HEAD`). **Everything is local and unpublished:** nothing was pushed, deployed, or posted; no
PAT, OAuth secret or DNS setting was touched; no contributor branch was edited. Known packaging note: the sdist excludes
`deploy/`, so the Containerfile is in the repository but not in the sdist; `__version__` is still `0.1.0`.

## F. Release readiness

**Ready for private testing.** **Not yet ready for a public alpha.** Concrete blockers:

1. The new image and profiles have not run on the real runner VM, and `odytor-runner check` has not been run on it.
2. CPU limits are unverified on this host.
3. No real remote MCP client has been tested for images, approvals or authentication, and ChatGPT's connector reportedly
   offers only OAuth or no authentication (documented, not verified), so the endpoint-protection decision is open.
4. No real GitHub write was exercised through `publish_apply`.
5. The deployed revision could not be verified; the app image is not yet build-stamped.

## G. Remaining work

Lint, type-check, build and npm-install profiles; database-migration and container-build profiles; Firefox/WebKit,
video and HAR; per-request mock timing and a header-click interaction check for the acceptance scenario; downloads, popups
and dialogs tests; cross-process concurrency tests; a Dockerfile build stamp and persistent volumes for staging; a real-client
compatibility pass; a controlled real `publish_apply` against a throwaway PR; an orchestrator, only if a concrete
unattended scenario justifies it (none was built).
