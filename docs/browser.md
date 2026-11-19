# Browser execution and visual evidence

`browser_run` drives Chromium against the application in a workspace, **inside the isolated runner**, and returns
step results, console and page errors, network evidence, accessibility snapshots and screenshots (as native MCP image
content). It builds on the same container boundary as `test_run`: no network, a read-only root, no capabilities, an
unprivileged user, resource limits, and a source tree that arrives as a credential-free tar.

## How a run works

1. The exact current workspace tree is exported and sent to the runner (`expected_version` must match).
2. In a fresh container the driver serves the tree on `127.0.0.1` (a static directory, or an operator-approved
   command), waits for readiness, and launches Chromium with a fresh context: fixed viewport, locale, timezone and
   color scheme, service workers blocked, downloads refused, no permissions.
3. The typed steps run in order. A failing step stops the run; the driver still captures a failure screenshot and a
   final snapshot.
4. Evidence is reported back; screenshots and the optional trace become artifacts.

## Steps

`goto` (a path such as `/index.html`), `click`, `fill`, `type`, `press`, `select`, `check`, `uncheck`, `hover`,
`wait_for`, `wait_ms`, `screenshot` (name), `snapshot`, `assert_visible`, `assert_hidden`, `assert_text`,
`assert_count`, `assert_requests` (requests to an exact path since the last `reset_network_counts`), `wait_requests` (wait until the application has issued N requests to a path; the race-free alternative to sleeping),
`reset_network_counts`. Selectors are Playwright selectors, at most 300 characters, strict (a selector matching several
elements is an error).

**Deliberately absent:** unrestricted JavaScript evaluation, file upload and download, arbitrary URLs, cookies and
storage editing, request interception by the caller. If any of these is added later it must be treated as arbitrary
code execution in the sandbox, which is what any page script already is.

`mocks` fake API responses (`path`, `json` or `body`, `status`, `delay_ms` up to 5 s, applied asynchronously so slow responses can be observed racing other actions); they are applied inside the browser, not
on a network.

## Sessions (bounded exploratory interaction)

The runner is stateless, so a session is **server-side history**: the first call returns a `session_id`; later calls
that pass it replay the accepted steps in a fresh sandbox (without recapturing their screenshots, and discarding their
console and network noise) and then perform the new steps. New steps join the history only when the run finished
cleanly. Consequences, all intended:

* no browser process or container exists between calls, so there is nothing to orphan;
* a session tests exactly one workspace tree (`expected_version` must equal the version it was opened on);
* each call costs a replay; a session holds at most 150 steps; a caller keeps at most 5 sessions and the oldest idle one is evicted when a sixth opens (so one-shot runs never lock anyone out); `browser_close` ends one explicitly;
* an application that behaves differently on replay produces `replay_diverged`, never a silently different state.

## Results you can trust

`status: passed` needs every step to succeed, a page to have loaded, and **at least one assertion among the new steps**.
Exploration without assertions is `completed` and is never green. `setup_error/startup_failure` (the app never
answered) is distinct from `failed/assertion_failed` and `failed/step_failed`; `timeout`, `canceled` and
`output_overflow` are distinct as well. If the browser stack hangs, a watchdog still returns the steps completed so
far and the result is a `timeout`.

## Network policy

Defence in depth, in order of strength:

1. **The container has no network route.** Nothing outside it is reachable regardless of what the browser does.
2. Chromium resolves nothing but `127.0.0.1` (`--host-resolver-rules`).
3. Every request is checked against the single local origin; redirects and subresources are checked individually.
   Blocked requests are recorded (`network.blocked`) with their destination.
4. Service workers are blocked, downloads refused, popups closed, dialogs dismissed.

WebSockets are not routed through Playwright (closing one inside its route handler deadlocks the synchronous API); they
cannot connect for the reasons in 1 and 2 and are recorded when attempted.

A browser context is **not** an operating-system security boundary; the container is. Treat the application under test
as untrusted code.

## Artifacts

Screenshots (`png`/`jpeg`), an optional Playwright trace (`trace: true`) and the application server's log. See
[artifacts](artifacts.md). Screenshots are returned as MCP image content alongside the structured result.

## Configuration

* **Image.** Build `deploy/runner/image/Containerfile` (Python harness, Node 22, Playwright 1.63 with Chromium on an
  Ubuntu base pinned by digest), load it on the runner host and pin it by digest in `ODYTOR_RUNNER_IMAGE`. The runner
  *observes* what the image contains (Python, Node, Playwright, Chromium); a profile whose tools are missing is reported
  `installed: false` and refused with `profile_unavailable`.
* **Policy.** Browser runs exist only for repositories whose maintainer policy enables them:

  ```toml
  [repos."owner/repo".browser-playwright]
  kind = "static"        # or "command" with an operator-approved argv
  root = "static"        # directory inside the tested tree
  port = 8765
  ready_path = "/index.html"
  ```

  Repository files cannot change this table, the image, the limits or the network policy.
* **Limits.** 60 new steps per call, 8 screenshots per run, 20 mocks, 64 KiB per mock body, 24 MiB of artifacts per run
  (16 MiB each), `ODYTOR_TEST_*` timeouts, and the runner's CPU, memory and PID limits.

## Known limitations

* Chromium only; no Firefox/WebKit, mobile emulation, video, or HAR.
* Replay-based sessions cost time proportional to history.
* Applications that need real backend services must provide them inside the sandbox via an approved `command`, or be
  exercised with `mocks`.
* Visual evidence is evidence, not proof: the page under test can render anything it likes.
