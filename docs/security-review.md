# Security review: workspaces, browser, artifacts, publication

This is the pre-release review of the remote-engineering expansion. Passing functional tests does not establish sandbox
security, so each line says what was **verified by a test or observation**, what was **reviewed by reading only**, and
what is **not verified**.

## Trust boundaries

```text
MCP client ──(authenticated)──▶ Odytor control plane (holds the GitHub credential; never executes repository code)
                                   │  workspace store (git objects), artifact store, plan/session/run records
                                   │  credential-free tar + validated job description, over a forced-command SSH channel
                                   ▼
                              Runner host (no GitHub credentials, no Odytor data, no route to production)
                                   │  one rootless container per job: no network, read-only root, no capabilities,
                                   │  user 65534, pinned image, CPU/memory/PID/tmpfs limits
                                   ▼
                              repository code, Node, Chromium (untrusted)
```

## Findings by area

| Area | Result |
|---|---|
| Authentication and authorization | Reviewed: every new tool derives the caller from the same admission rules as `github_client` (`caller_principal`); workspaces, plans, sessions, runs and artifacts are owner-bound and unknown/foreign/expired ids are indistinguishable. Verified by tests for ownership on each store. |
| Workspace path confinement | Verified: traversal, absolute, backslash, NUL, `.git`, glob and symlink-parent attacks are rejected for every operation; no checkout exists on the control plane (a test asserts the repository directory contains only `.git`). |
| Git subprocess arguments | Verified/reviewed: paths follow `--`, pathspecs are literal (`GIT_LITERAL_PATHSPECS`), selectors cannot start with `-` or contain glob metacharacters, no shell is used, hooks/filters/config/credentials are not inherited, auto-gc is off. |
| Arbitrary code execution boundary | Control plane executes none: it parses Python with `ast` (never imports or runs it) under a 256 KiB bound. All execution is in the runner. |
| Runner isolation | Verified on this host with a real rootless Podman: unprivileged user, no capabilities, no network, read-only root, and Chromium launches under exactly those restrictions. Memory and PID limits observed enforced; **the CPU controller is not delegated on this development host, so CPU limiting is NOT verified here**. `odytor-runner check` and the capability probe report this honestly. |
| Container privilege configuration | Reviewed: unchanged flag set for test containers; the browser job adds no mounts, no capabilities, no devices. `--no-sandbox` is Chromium's in-container default; the container is the boundary. |
| Host and internal network isolation | Verified: a page that fetches the cloud metadata address, an external host, another local port and an external WebSocket is blocked and reported; the container has no network route at all. **Not verified:** the production runner VM's firewall and routing (outside this checkout). |
| Dependency installation | Unchanged and still network-bearing only for the `python-pytest` preparation container, from maintainer-approved pinned requirements. The new profiles install nothing. Node/npm dependency installation is **not implemented**. |
| Cache integrity | Unchanged (read-only mounts, digest-keyed environments, locks). No new shared cache was added. |
| Resource exhaustion | Verified: workspace count/size/file/checkpoint caps, artifact per-file/per-run/per-caller/global caps, report-size ceilings, step/mock/screenshot caps, queue depth, run timeouts, a driver watchdog. Reviewed only: disk-pressure behavior beyond the free-space checks. |
| Browser navigation restrictions | Verified for fetch, XHR, WebSocket, redirects to blocked origins, service workers; downloads/popups/dialogs are refused in code (the downloads and popup paths have no dedicated test). |
| Artifact retrieval | Verified: opaque ids, owner binding, content sniffing ignores runner claims, active content is returned only as inert labelled text, images only if the bytes are an image, bounded windows. |
| Concurrent session ownership | Verified: threaded workspace edits never overwrite each other; sessions and plans are per-owner. A cross-process lock is used but only thread concurrency is tested. |
| Cancellation and orphan cleanup | Verified with real containers: cancelling a running test or browser run removes the container, its descendants (including Chromium) and the runner job directory. A host-level check confirmed no sandbox browser process remains and that the check can see one when it exists. |
| Publication authorization | Verified: no write without `user_authorized=true` and an exact plan-bound confirmation; workspace change, patch change or head movement after planning is refused before any write; at-most-once with durable intent; reconciliation of lost responses; fork pushes are leased fast-forwards. The approval itself is the caller's to obtain: the server cannot prove a human said yes. |
| Revision binding | Verified: workspace results, runs, artifacts and plans all carry the exact tree; stale versions are refused. |
| Secret handling | Reviewed: the GitHub token is passed to Git per command through the environment, never written into a workspace, never in the runner environment, and `safe_payload` redacts it from responses. A repository secret scan of committed files is part of the release checklist. |

## Defects found and fixed during this work (evidence that the tests have teeth)

1. A test-less Node file was reported as a passing test (Node counts a loaded file as one pass). Now excluded and
   classified `no_tests_collected`.
2. `safe_payload` silently truncated 64 KiB text windows, base64 chunks, accessibility snapshots and diff patches at
   8 KiB. Each tool now declares its own bound.
3. Closing a WebSocket inside Playwright's route handler deadlocked the browser driver; replaced by observation plus the
   container's network isolation, and a watchdog added so a future hang still returns evidence.
4. Node 22 treats a directory positional as a module; directory selectors now expand to bounded globs.

## Residual risks and limits

* Container isolation is not proof against kernel or runtime escapes. The runner host remains the real boundary: keep it
  single-purpose, patched, credential-free and without a route to production.
* Evidence produced inside the sandbox can be forged by the code under test; results are consistency-checked, not proven.
* The application under test shares a container, a PID namespace and a UID with the driver and Chromium.
* Replay-based sessions assume the application is deterministic; divergence is detected, not prevented.
* CPU limiting is unverified on this host; production must pass `odytor-runner check`.
* Not implemented: unrestricted JS evaluation (intentionally), npm dependency installation, lint/typecheck/build
  profiles, database migration tests, container-build validation, video.

## Release posture

Suitable for **private testing**. Before a public alpha: run the new image on the real runner VM and pass
`odytor-runner check`, test at least one real remote MCP client end to end (images, approvals, authentication), review
the production deployment's endpoint protection, and complete the unverified items above.
