# Capability matrix

Verified against the source at `a92c4c6` (the revision the owner named as previously deployed) and kept current
on the `feat/remote-engineering-workspace` branch. "Exposed" means reachable as an MCP tool.
Production was **not** inspected: this checkout has no way to read the deployed revision (see "Open facts").

| Capability | State at `a92c4c6` | Evidence |
|---|---|---|
| PR evidence, CI diagnosis, additive quick fix, conflict inspection, rebase, CI control | Exposed and operational | `odytor/server.py` registers six tools |
| `test_run` / `test_result` (pytest on exact head or prospective merge, optional test overlay) | Exposed; operational only with a configured runner | `RunSettings.load()` returns `None` without `ODYTOR_TEST_RUNNER_COMMAND` |
| Overlay can add new regular test files | Implemented and exposed | `overlay.py` accepts `new file mode 100644` under policy test dirs |
| Overlay failures name their cause | **Missing** (one generic header error for five different defects) | reproduced; fixed on this branch |
| Prospective merge against the live target tip | Exposed in `test_run`; **missing** in `pr_conflicts` (snapshot base only) | `pr_conflicts.get_pr_conflicts` merged `context['base_sha']` |
| Job cancellation | Implemented internally, **not exposed** | `JobService.cancel()` has no MCP caller |
| Job state, result retrieval | Exposed (`test_result`, test runs only) | |
| Execution profiles | One (`python-pytest`), hard-coded in the runner | `profile.PROFILES`, `sandbox.validate_header` |
| Runner facts for discovery | **Missing**; `odytor-runner check` exists but the SSH forced command cannot be given arguments by the control plane | `deploy/runner/git-plus-runner-entry` |
| Scratch workspaces (read, search, edit, diff, reset) | **Missing** | no workspace code |
| Browser execution, screenshots, traces | **Missing** | no Playwright anywhere in the package |
| Artifact store, native MCP image delivery | **Missing** | |
| Guarded publication of scratch changes | **Missing** (only `quick_fix` for a pasted patch to an existing PR) | |
| Durable session records | Partial: one JSON file per test run (`RunStore`) | `jobs.py` |
| Client approval prompts per tool call | **Client-dependent** | see `docs/client-compatibility.md` |
| Remote MCP authentication choice | **Client-dependent** (OAuth vs static bearer) | `auth.create_auth` |

## State after this branch (`feat/remote-engineering-workspace`)

PASS means exercised by an automated test; real-sandbox rows used real rootless Podman and, for the browser, real Chromium.

| Capability | State | Evidence |
|---|---|---|
| Stage-specific overlay diagnostics (stage, line, excerpt, declared vs found hunk counts) | Exposed | PASS: `tests/test_overlay.py`, using real `git diff` output |
| `pr_conflicts(merge_target="live_tip")` | Exposed | PASS: `tests/test_pr_conflicts.py` |
| `odytor_capabilities` + runner probe frame | Exposed | PASS: `tests/test_capabilities.py`, real runner probe |
| Disposable workspaces: create (head, merge, branch), read, search, symbols, edit, diff, checkpoint, reset, close, expiry, caps | Exposed | PASS: `tests/test_workspace*.py` incl. threaded concurrency |
| Edit then test in a real container, tested tree named, immutable snapshot | Exposed | PASS: `tests/test_workspace_e2e.py` |
| `test_cancel` tears down container, descendants and runner job directory | Exposed | PASS: real containers (pytest and browser runs) |
| Profiles: `python-pytest`, `node-test`, `browser-playwright` | Exposed | PASS: `tests/test_profile*.py`, `tests/test_harness_tap.py`, `tests/test_browser_e2e.py` |
| Python lint, type check; Node lint, type check, build; npm dependency installation | **Missing** | not implemented |
| Database migration tests, container-build validation | **Missing** | not implemented |
| Browser: serve, drive, assert, network and console evidence, a11y snapshots, screenshots, traces, sessions | Exposed | PASS (Chromium 153 in the sandbox) |
| Browser network restrictions (metadata, internet, other ports, WebSocket, service workers) | Exposed | PASS (page-side attempts blocked and reported) |
| Browser downloads, popups, dialogs refusal | Implemented | NOT TESTED individually |
| Unrestricted JavaScript evaluation, video, uploads | **Deliberately absent** | |
| Artifacts: store, quotas, provenance, native image delivery, chunked fallback | Exposed | PASS: `tests/test_artifacts.py`, real screenshots through a real MCP client |
| Image display in Claude Code, ChatGPT, Perplexity, Codex | Client-dependent | NOT TESTED (see client-compatibility) |
| Guarded publication (plan, apply, idempotency, reconciliation, fork lease) | Exposed | PASS against a fake GitHub client and real local git; **no real GitHub write was performed** |
| Durable records: workspaces, runs, sessions, plans, artifacts survive restart | Implemented | PASS for workspaces; others by construction (atomic JSON) |
| Per-client approval prompts | Client-dependent | NOT TESTED |
| Remote MCP auth for ChatGPT / Perplexity | Client-dependent | DOCUMENTED only (see client-compatibility) |

## Open facts

* **Production revision** is not observable from this checkout. `ODYTOR_BUILD_REVISION` is reported by
  `odytor_capabilities` once the image is stamped with it.
* **CPU limiting** depends on the runner host delegating the `cpu` controller. The development machine delegates only
  `memory` and `pids`, so CPU limits are **not verified here**; the probe reports the host as `degraded`, which makes
  `isolated_execution.available` false even though jobs run. That is intentional: "operational" is what was observed.
