# Security Policy

## Supported versions

OdyTor `0.1.0` (v1 release baseline) is pre-1.0 source-available software. Security fixes are applied to the current release line on `main`. There are no LTS releases, legacy patch branches, or backport commitments.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.** Public issues expose exploit details before a fix is available.

Use **GitHub Security Advisories / private vulnerability reporting** on the repository (`Security -> Report a vulnerability`). If private reporting is unavailable on the repository at the time of your report, open a minimal public issue requesting a private contact channel without disclosing exploit details or target infrastructure.

Please include:

- Affected component (MCP server/tool, Git workspace handling, `odytor-runner` container sandbox, SSH forced-command setup, authentication/allowlist layer, or deployment configuration).
- Steps to reproduce, including the deployment mode (`stdio` or `streamable-http`), configuration settings, and minimal inputs.
- Security boundary impacted and potential impact under those conditions.
- A suggested mitigation or patch, if available (optional).

## Response expectations

Reports are reviewed on a best-effort basis by the maintainer. No guaranteed response SLA is committed at this stage of the project. Reports with clear reproduction steps and boundary analysis are triaged fastest.

## No bug bounty

OdyTor does not operate a bug bounty program.

## Security boundaries in scope

OdyTor's architecture relies on explicit isolation and authorization boundaries documented in [docs/security.md](docs/security.md) and reviewed in [docs/security-review.md](docs/security-review.md):

- **No in-process repository execution**: The MCP control plane never executes untrusted repository code, tests, build scripts, or Git hooks.
- **Isolated runner boundary**: `odytor-runner` executes repository tests and headless Chromium inside rootless containers with `--network=none`, `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, unprivileged UID `65534`, and no GitHub credentials.
- **Strict revision pinning and leases**: Mutating GitHub operations pin exact `expected_head` and `expected_base` commits and use `--force-with-lease` on fork pushes.
- **Allowlists and caller binding**: `ODYTOR_ALLOWED_REPOSITORIES` and `ODYTOR_ALLOWED_GITHUB_USERS` constrain repository and OAuth access; workspaces, artifacts, test runs, and publication plans are strictly bound to the owning caller principal.
- **Secret isolation**: GitHub tokens and OAuth secrets remain in memory and are stripped from runner environments, tool payloads, and logs.
- **Guarded publication gates**: `publish_apply` and `pr_body_apply` are disabled by default (`ODYTOR_WORKSPACE_ALLOW_PUBLISH_APPLY=off`) and require explicit plan confirmation plus `user_authorized=true`.

Vulnerabilities that break any of these boundaries, allow path/workspace escape, bypass allowlists or principal isolation, leak tokens or cross-caller artifacts, or bypass container/network isolation on the runner are in scope.
