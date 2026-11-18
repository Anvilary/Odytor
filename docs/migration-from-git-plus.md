# Migration from Git Plus to Odytor

Odytor is the new name of Git Plus. This is a naming change: the eight tools, their arguments and annotations, the
authorization model and the isolated runner are unchanged (a script that compares every tool name, argument schema,
annotation and output schema between the last Git Plus build and this one finds only the server display name differing).
This page is temporary and is deleted before the first public release.

## Identity

| | |
| --- | --- |
| Product | Odytor, an Anvilary Tools product |
| Python distribution and package | `odytor` (not registered on PyPI when checked on 2026-11-18; recommended fallback `anvilary-odytor` if it is taken before publication; nothing has been published) |
| Commands | `odytor`, `odytor-runner` |
| MCP server name | `Odytor`; tool names are unchanged and unbranded |
| Settings prefix | `ODYTOR_` |

## Collisions with the older Odytor (do not overwrite)

A separate, older terminal application called Odytor is installed and running. It is a different project and is not touched.

| Where | What exists | Consequence |
| --- | --- | --- |
| `~/.local/bin/odytor`, `odytor-mcp`, `odytor-legacy`, `odytor-minimal` | wrappers around `~/Projects/odytor-stabilization` and `~/Projects/Odysseus/odytor` | The new launcher cannot be installed as `~/.local/bin/odytor-mcp`. The Odytor package is installed only into its own virtualenv, whose `bin/` is not on `PATH`, so its `odytor` command does not shadow the older one. |
| Agent Hub `mcp/retired-servers.json` | lists `odytor` | `agent-sync mcp` removes any shared MCP server named `odytor` from every client. A shared entry with that name needs that line removed first. |
| hosting-bay | containers `odytor-mcp`, `odytor-operator`, `odytor-worker`, images `odytor-mcp:*`, volumes `odytor_*` and `odytor-stage_*`, networks `odytor_*`, directories `~/services/odytor` and `~/services/odytor-stage` | Nothing of the old stack is reused. The new image is `odytor:<commit>`; volumes, networks and directories keep their existing `git-plus` names. |

## Temporary compatibility

* **Settings.** `ODYTOR_X` is canonical and `GIT_PLUS_X` is still read for every setting (server, test runner and runner
  host). If both are set with different values, startup fails with a message naming both; nothing is guessed, so a stale old
  value can never silently widen a repository allowlist. Equal values are accepted. A start-up note on stderr lists any
  old-prefix settings still in use. The old `GIT_PLUS_ODYSSEUS_PAT*` aliases for the server-held PAT also remain, as before.
* **Commands.** `git-plus` and `git-plus-runner` are entry-point aliases of the same functions (no second implementation).
  An installation made before the rename also keeps its old console scripts until it is reinstalled.
* **State.** Default state directories still resolve to existing data: `~/.local/state/git-plus/tests` and
  `~/.local/state/git-plus-runner` are used when present, otherwise the `odytor` equivalents. A runner host therefore keeps
  its dependency cache.
* **Runner host.** The forced command prefers `odytor-runner` and falls back to `git-plus-runner`, and `runner.env` may use
  either prefix (re-provisioning rewrites it with `ODYTOR_` names and keeps a `runner.env.before-<time>` copy for rollback).

All of the above is removed before the first release. Existing deployments should move to the new names first.

## Identifiers deliberately not renamed

They identify existing deployments and data; renaming them would orphan or break something, and none is branding.

| Identifier | Why it stays |
| --- | --- |
| Production container `git-plus`, directory `~/services/git-plus`, Compose project `git-plus` | The Nginx upstream and the Cloudflare tunnel address the container by name; a new project name would create new volumes and networks |
| Volumes `git_plus_data` (OAuth state) and `git_plus_test_data`, container home `/home/git-plus` | Persistent data; the production `.env` pins `HOME` and `FASTMCP_HOME` there |
| Hostname `git-plus.alexandreteixeira.dev` | In use by ChatGPT and other clients; see the endpoint plan below |
| Runner VM container `git-plus-runner-vm`, network `gp-runner-net`, account `gp-runner`, `/opt/git-plus-runner`, `/etc/git-plus-runner`, `git-plus-runner-gc.timer`, firewall table `git_plus_runner` | Existing runner host and its SSH trust |
| Runner container prefix `gp-` and label keys `git-plus.root`, `git-plus.workspace` | Runtime markers of running jobs; orphan cleanup depends on them |
| Repository `Anvilary/git-plus` and the local checkout `~/Projects/git-plus` | Reserved for the repository rename |

## Local agents (12 profiles through Agent Hub)

State: the installed launcher `~/.local/bin/git-plus-mcp` and the shared entry `git-plus` are unchanged and still work. The
new launcher is `deploy/local/odytor-mcp` (same credential handling, fixed allowlist, clean stdout and Antigravity D-Bus
behavior). It is not installed, because `~/.local/bin/odytor-mcp` belongs to the older Odytor.

To switch, once a decision on the collision is made (for example installing the new launcher under another name, or moving
the older one aside yourself):

1. Reinstall the checkout's virtualenv from the renamed source (`uv pip install -e .`); until then the old launcher keeps
   running the old code.
2. Back up `~/.config/agent-hub/mcp/shared-servers.json` and `retired-servers.json`, plus each client's generated config.
3. Install the launcher, change the shared entry name and `command` (one entry, never both `git-plus` and `odytor`),
   remove `odytor` from `retired-servers.json` only if the shared server must be named `odytor`, and add `git-plus` to
   that list so the old entry is removed everywhere.
4. `agent-sync mcp --dry-run`, then `agent-sync mcp`, then `agent-sync verify`.
5. Rollback: restore the two hub files and the launcher name, and run `agent-sync mcp`.

## Hosted deployment

Ready now: image `odytor:<commit>` from `deploy/app/Dockerfile` (entrypoint `odytor`), and `deploy/app/compose.yaml.template`
(new `ODYTOR_TEST_*` names, container and project names unchanged). The current production `.env` and Compose file work with
the new image unchanged because the old settings are read. To migrate the settings, rename the keys in `.env`
(`GIT_PLUS_` to `ODYTOR_`, values untouched) in the same change as the image; do not leave both spellings with
different values.

The existing endpoint stays. A second hostname for the same application is not a routing change only: the OAuth issuer and
resource URL come from the single configured public URL, so ChatGPT connections created for the old hostname are bound to
it. Activating `odytor.alexandreteixeira.dev` therefore needs separate authorization and these steps: a Cloudflare ingress
rule and DNS record, an Nginx `server_name`, the GitHub OAuth App callback for the new host, a new connector in each client,
and a decision on which host is the OAuth issuer. Nothing of this is configured.

## After the GitHub repository is renamed to `Anvilary/odytor`

1. Local checkout: `git remote set-url origin git@github.com:Anvilary/odytor.git` (GitHub redirects the old URL meanwhile).
2. Repository allowlists name the repository: change `Anvilary/git-plus` to `Anvilary/odytor` in
   `deploy/local/odytor-mcp` (and the installed launcher) and in the production `ODYTOR_ALLOWED_REPOSITORIES`. Until then
   requests naming the new repository are refused by the allowlist.
3. The test fixtures that use `Anvilary/git-plus` as a sample repository name need no change.
4. If the checkout directory is renamed, set `ODYTOR_HOME` for the launcher or update its default.
5. Add the repository URL to the package metadata (`[project.urls]`) when it exists; nothing in the code embeds it.
6. CI, the tunnel, OAuth and the runner do not depend on the repository name.
