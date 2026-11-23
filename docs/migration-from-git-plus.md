# Migration from Git Plus to OdyTor

OdyTor is the canonical name of the project previously developed as Git Plus. In OdyTor `0.1.0`, the original tool names, schemas, authorization boundaries, and isolated runner protocol remain compatible while the `ODYTOR_*` configuration namespace and `odytor` / `odytor-runner` commands are standard.

## Identity

| | |
| --- | --- |
| Product | OdyTor, an Anvilary Tools project |
| Python distribution and package | `odytor` |
| Commands | `odytor`, `odytor-runner` |
| MCP server name | `Odytor` |
| Settings prefix | `ODYTOR_` |

## Compatibility Retained in 0.1.0

For existing deployments upgrading from Git Plus, OdyTor `0.1.0` retains the following compatibility paths (scheduled for removal in a future release):

* **Settings**: `ODYTOR_X` is canonical, and `GIT_PLUS_X` is still accepted for server, test runner, and runner host settings. If both `ODYTOR_X` and `GIT_PLUS_X` are set with different values, startup fails closed and names both variables rather than guessing. Equal values are accepted. Local `stdio` starts print a deprecation notice to `stderr` when legacy names are active; hosted HTTP starts do not. The legacy `ODYTOR_ODYSSEUS_PAT*` and `GIT_PLUS_ODYSSEUS_PAT*` aliases for single-user server-held PAT mode also remain supported as aliases of `ODYTOR_SERVER_PAT*`.
* **Commands**: `git-plus` and `git-plus-runner` remain installed console script entry points pointing to the exact same `odytor.cli:main` and `odytor.sandbox:main` functions.
* **State directories**: Default state directories fall back to `~/.local/state/git-plus/tests` and `~/.local/state/git-plus-runner` when those legacy directories exist and the `odytor` equivalents do not yet exist.
* **Runner host**: The forced-command entry script prefers `odytor-runner` and falls back to `git-plus-runner`, and `runner.env` accepts either prefix (`provision.sh` rewrites retained `GIT_PLUS_RUNNER_*` lines to `ODYTOR_RUNNER_*` and keeps a timestamped backup).

## Legacy Deployment Identifiers

Existing self-hosted deployments created under Git Plus may retain internal infrastructure identifiers (such as container names `git-plus` / `git-plus-runner-vm`, Docker volumes `git_plus_data` / `git_plus_test_data`, runner account `gp-runner`, `/opt/git-plus-runner`, `/etc/git-plus-runner`, and container label keys `git-plus.*`) to avoid orphaning persistent volumes or breaking existing reverse-proxy upstreams during upgrades.

## Migrating Existing Installations

1. Reinstall the package in your virtual environment (`pip install -e .` or install the `0.1.0` wheel).
2. Update your MCP client configuration to launch `odytor --transport stdio` (or [deploy/local/odytor-mcp](../deploy/local/odytor-mcp) for `gh` CLI token injection).
3. Replace `GIT_PLUS_*` environment variables with `ODYTOR_*` equivalents in `.env` and `runner.env` (and remove duplicate legacy entries so conflicting values cannot block startup).
4. In hosted Streamable HTTP deployments, ensure `ODYTOR_PUBLIC_URL` matches your canonical HTTPS endpoint (for example `https://odytor.example.com`) and that your GitHub OAuth callback URL matches `${ODYTOR_PUBLIC_URL}/auth/callback`.
