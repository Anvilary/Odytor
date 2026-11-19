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

## Added on this branch

See `git log` for the authoritative list; each row is updated when its commit lands.

| Capability | State | Verified by |
|---|---|---|
| Stage-specific overlay diagnostics | Implemented | `tests/test_overlay.py` (real `git diff` output) |
| `pr_conflicts(merge_target="live_tip")` | Exposed | `tests/test_pr_conflicts.py` |
| `odytor_capabilities` and runner probe frame | Exposed | `tests/test_capabilities.py`, real runner probe in `tests/test_sandbox_integration.py` |

## Open facts

* **Production revision** is not observable from this checkout. The image has no build stamp today;
  `ODYTOR_BUILD_REVISION` (a 40-hex commit) is now reported by `odytor_capabilities` once the image is built with it.
* **Resource enforcement** depends on the runner host delegating the `cpu`, `memory` and `pids` cgroup controllers.
  The probe reports this honestly; the development machine delegates only `memory` and `pids`.
