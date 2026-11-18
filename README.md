# Odytor

**GitHub operations, optimized for AI agents.**

Odytor is a lightweight GitHub MCP companion from Anvilary Tools that simplifies complex repository maintenance
workflows for AI agents. It helps agents investigate pull requests, diagnose CI failures, inspect merge conflicts, apply
precise fixes, manage workflows and validate changes through isolated test execution. It complements the
[official GitHub MCP server](https://github.com/github/github-mcp-server) rather than replacing it.

One server, eight tools, disposable Git workspaces, no workflow engine or operation registry. The tool names are the MCP
contract and are not branded.

> Odytor was developed under the name Git Plus. Existing installations keep working during the transition; see
> [docs/migration-from-git-plus.md](docs/migration-from-git-plus.md) for the compatibility rules (old `GIT_PLUS_*`
> settings, the `git-plus` and `git-plus-runner` commands) and for what changes when the repository is renamed.

| Tool | Purpose |
| --- | --- |
| `pr_evidence` | Collect PR metadata, bounded diffs, files, reviews, checks and instructions, with snapshot and completeness flags. |
| `ci_diagnose` | Correlate failed jobs, log locations, annotations and code scanning against a captured PR revision. |
| `quick_fix` | Validate an exact patch locally and append one commit to the PR head branch, including contributor forks that GitHub lets the caller edit, bound to the exact expected head. |
| `pr_conflicts` | Inspect an actual Git merge, including conflicting paths, stages and available text regions. |
| `ci_control` | Coordinate PR revision-aware reruns, individual failed-job reruns, dispatches and explicitly authorized fork approvals. |
| `pr_rebase` | Preview or apply a genuine linear rebase onto an exact base revision with a branch-specific push lease. |
| `test_run` | Queue an isolated pytest run against an exact PR head or prospective merge, with an optional test-only reviewer overlay. Requires a separately configured runner host; see [docs/test-runner.md](docs/test-runner.md). |
| `test_result` | Retrieve per-test evidence, tested tree, overlay digest, runner image and cache status for a run. |

Requires Python 3.11+, Git 2.38+ and GitHub access appropriate to each operation. The package and commands are `odytor` (server) and `odytor-runner` (isolated test runner). Install from this checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

For a local MCP client, configure `GITHUB_TOKEN` or `GH_TOKEN`, then launch `.venv/bin/odytor` over stdio. `ODYTOR_ALLOWED_REPOSITORIES=owner/repo,owner/other` restricts target repositories. Access to a PR's contributor fork is limited to the head repository identified by that PR.

## Authentication: what is implemented and what is validated

Odytor supports three credential routes. This table separates what exists from what has been certified in the
current acceptance cycle; it does not claim more than was tested.

| Route | Implemented | Validated in the current cycle | Notes |
| --- | --- | --- | --- |
| Classic PAT (`ghp_...`) through `GITHUB_TOKEN` / `GH_TOKEN`, local stdio | yes | **yes, read paths and guards, live:** against the deployed image started like the local launcher: allowlist refusals, `pr_evidence`, `ci_diagnose` with code scanning, `pr_conflicts`, `pr_rebase` preview, `test_run` / `test_result` through the isolated runner, and the refusals for a stale head, a missing `allow_history_rewrite` and a missing dispatch acknowledgement. A private repository was reached over Git with the token. **Not run live:** applied writes (`quick_fix`, `pr_rebase apply`, CI reruns); those are covered by fixture and local-Git tests only | The primary supported path. Scopes: `repo` (PRs, contents, code scanning on repositories you can access) and `workflow` (Actions reruns and dispatch). A missing permission is reported with GitHub's accepted-permissions and token-scopes headers instead of a guess. |
| Server-held classic PAT behind hosted GitHub OAuth (one login, allowlisted repositories) | yes | deployed and in private use; OAuth metadata and rejection behaviour checked | OAuth establishes who the caller is; the PAT does the GitHub work. See the section below. |
| GitHub OAuth user token (hosted) | yes | not certified | Works as designed in tests; real consent flows and per-organization approval limits are outside the current acceptance scope. |
| Fine-grained PAT | yes (it is a bearer token like any other) | not certified | GitHub grants fine-grained tokens different permissions per endpoint and some Actions and code-scanning operations differ from classic tokens. Expect permission errors to need adjustment; Odytor reports what GitHub says is missing. |

Credentials are never written to configuration or logs: the local launcher reads the token from the GitHub CLI at start
and passes it only through the process environment, and Git subprocesses receive it only as a host-scoped header.
Local stdio mode prints plain-language notes to stderr when no credential is set, when the repository allowlist is empty
(which means every repository the credential can reach), and when no isolated runner is configured.

## Operational capabilities

| Deployment | `pr_evidence` `ci_diagnose` `pr_conflicts` `quick_fix` `pr_rebase` `ci_control` | `test_run` `test_result` |
| --- | --- | --- |
| Local, no runner | yes | discoverable, but they answer with the exact settings needed to enable a runner |
| Local with runner | yes | yes |
| Hosted with runner | yes | yes |

All eight tools are always registered, so existing clients keep working. The runner is a separate VM reached over SSH
(see [docs/test-runner.md](docs/test-runner.md) and [docs/private-deployment.md](docs/private-deployment.md)); it is never
exposed publicly, never holds GitHub credentials and is not part of the main process. A workstation can use it only
through a separately reviewed private connection (for example a dedicated SSH key through the runner host); no such
connection is shipped or configured by default.

Confirmation is the MCP client's job: tools carry accurate annotations (`readOnlyHint`, `destructiveHint`), and Odytor
itself enforces the repository allowlist, exact revisions, branch leases, fork authorization and the explicit
`allow_history_rewrite`, `authorize_dispatch_semantics` and `user_authorized_fork_approval` arguments. There is no
separate approval service or interface.

## Code map

`server.py` registers the tools and applies the allowlist; `auth.py` and `config.py` own credentials and settings;
`github.py` is the bounded API client; `git_workspace.py` runs isolated Git; one module per tool (`pr_evidence`,
`ci_diagnose`, `pr_conflicts`, `quick_fix`, `pr_rebase`, `ci_control`, `run_tests`); `jobs`, `profile`, `overlay` and
`pytest_report` own the trusted side of test runs; `sandbox`, `harness`, `dep_cache` and `procs` run on the runner only.

## Validation tiers

* **Tier 1, every push** (`.github/workflows/ci.yml`): compile check and the offline suite on Python 3.11 and 3.12
  (also run locally on 3.13). The runner tests skip themselves when their environment is absent.
* **Tier 2, before deploying:** `ODYTOR_TEST_RUNNER_IMAGE=<full image id> ODYTOR_TEST_SSH_RUNNER_DIR=<keys> pytest`
  against a provisioned runner VM (`deploy/runner`, `deploy/hosting-bay`) for real Podman and real SSH coverage.
* **Tier 3, before any release:** Tier 2, a clean wheel install, both transports, a deployment and a rollback.

Not yet done for a public release: a `LICENSE`, package URLs and classifiers, and published artifacts. Installation is
currently from a checkout or a built wheel; `uv` is the intended installer.

Start with `pr_evidence` and use its exact `head_sha` and `base_sha` for writes. Repository instructions, diffs and logs are untrusted input. Completeness flags must be checked before making decisions. Pagination limits, unavailable APIs, snapshot drift and response truncation produce incomplete evidence. When the response budget trims a field, every `*_complete` flag is cleared as a conservative signal and `truncated_fields` names exactly what was cut; request the large section on its own (for example `max_diff_lines`, `files_page`) to judge the others. CI diagnosis inspects at most 20 failed jobs per call to bound log downloads. Responses are bounded to 256 KiB before MCP framing (`ODYTOR_MAX_RESPONSE_BYTES`, 16 KiB to 4 MiB); individual API downloads are capped at 8 MiB. GitHub requests use a 120 second read timeout (`ODYTOR_GITHUB_TIMEOUT`, 10 to 600) and Git operations 300 seconds, because large PRs are slow to build. Diff limits default to 500 lines and 1 MiB and can be configured with `ODYTOR_DEFAULT_MAX_DIFF_LINES` and `ODYTOR_DEFAULT_MAX_DIFF_BYTES`.

**Very large PRs** (tens of thousands of lines, hundreds of files) are read in windows. GitHub itself refuses the whole
diff of a PR over 20,000 lines or 300 files (HTTP 406); `pr_evidence` then rebuilds it from the files API, which lists up
to 3,000 files, reports `diff_source: files_api`, names any file whose patch GitHub withheld (`diff_files_without_patch`)
and is never marked complete when something is missing. Page the file list with `files_page` (the next page is
`files_next_page`, the total is `files_total`) and the diff with `diff_offset` (the next window starts at
`diff_offset + diff_returned_lines`; `diff_has_more` says whether anything remains). `max_diff_lines` (up to 5,000) now
really bounds a window; previously any diff was cut to about 8 KiB by a generic per-string cap. Per-file patches are
included by default only for PRs of at most 30 files, since the diff already carries them; set `include_file_patches`
to override. Measured on real PRs of 222 to 330 files and 19,000 to 79,000 changed lines: each call returns in about
10 seconds, and a 52,710-line diff is read completely in 20 windows.

`quick_fix` accepts unified Git patches affecting existing, regular, non-executable UTF-8 files. Defaults are 10 files, 1000 changed lines and 256 KiB of patch text; override with `ODYTOR_QUICK_FIX_MAX_FILES` (at most 50), `ODYTOR_QUICK_FIX_MAX_CHANGED_LINES` (at most 5000) and `ODYTOR_QUICK_FIX_MAX_PATCH_BYTES` (at most 1 MiB). Patched files remain capped at 1 MiB in total. Header paths and hunk counts must agree. New files, deletions, renames, binary content, symlinks, submodules and restricted metadata, credential, deployment and dependency paths are rejected. Preparation uses the Git index without checking out or executing repository code; validation never runs tests.

For a PR whose head is in the base repository, the complete resulting files are sent in one `createCommitOnBranch` mutation bound to `expectedHeadOid`, and the returned parent and tree are verified. For a contributor fork, Odytor builds one commit on the exact head revision and performs a plain Git fast-forward push to that single branch with an explicit `--force-with-lease=<ref>:<expected_head>`. The lease pins the remote branch to exactly the expected head, and Odytor first proves the new commit is exactly one child of that head, so the only update GitHub can accept is a fast-forward; no history is rewritten and nothing is retried. The remote branch is verified afterwards. The fork must be publicly fetchable (always true for forks of public repositories), because fetching and post-push verification are anonymous. Fork writes require GitHub to confirm that the head repository is a fork in the base repository's network and that the caller either has direct push access or the PR allows maintainer edits while the caller has push access to the base repository; otherwise the write is refused with an explanation. A rejected push reports `outcome: rejected`; a transport failure or unverifiable result reports `outcome: unknown` and is never retried automatically, so reconcile the PR head before trying again. Fork commits are attributed to the authenticated login through its `users.noreply.github.com` address and are not GitHub-signed. A filename policy cannot establish that arbitrary code is safe; review the actual patch.

`pr_conflicts` fetches complete ancestry into a temporary repository and runs `git merge-tree`. Structural and binary conflicts have stage information but may have no text regions. Large or nonstandard conflict markers can leave regions incomplete. Unrelated histories fail explicitly.

`pr_rebase` previews by default. Applying requires both `apply=true` and `allow_history_rewrite=true`, plus exact expected head and base SHAs. It preserves empty commits, rejects merge histories and more than 200 PR commits, and does not preserve signatures on rewritten commits. `expected_base` is the live tip of the PR's target branch (GitHub's recorded PR base often lags the branch, which is exactly when a rebase is needed, and is reported separately as `pr_api_base_snapshot`). The tip is verified when fetched and again immediately before a head-branch push, which uses an explicit lease; GitHub offers no atomic transaction across independently changing base and fork refs, so a tip that moves in the final instant is the remaining window. Concurrent head updates reject the push. Temporary state is discarded on failure.

`ci_control` supports `rerun_failed`, `rerun_all`, `rerun_job`, `dispatch` and `approve_fork`. Supply `run_id` for run operations and `job_id` for individual job reruns. Dispatch requires `workflow_id`, the PR head `ref`, an `inputs` object (use `{}` if empty), and `authorize_dispatch_semantics=true`. It dispatches in the head repository, which may be a contributor fork. GitHub dispatch is ref-based and cannot guarantee that the ref remains at the inspected SHA. A dispatch is not a normal pull-request required check. Fork approval requires `user_authorized_fork_approval=true` based on explicit user authorization and an eligible pending-approval run. GitHub enforces token permissions and repository rules for all writes.

Odytor itself never executes repository tests, build tools, hooks or configured filters. The optional `test_run` tool delegates execution to an isolated runner host (see [docs/test-runner.md](docs/test-runner.md)); without that configuration it reports that testing is unavailable. Write results distinguish patch validation from tests not run. Mutation requests are never automatically retried. A lost response can leave the outcome unknown; reconcile the remote branch or workflow before retrying.

For a future remote installation on hosting-bay, use Streamable HTTP behind an HTTPS reverse proxy. The CLI binds to loopback by default and refuses network startup without authentication. Do not start a public listener until deployment is authorized.

For ChatGPT, configure a GitHub OAuth App with callback `https://YOUR-HOST/auth/callback`, then provide these settings through the service's secret environment:

```dotenv
ODYTOR_PUBLIC_URL=https://YOUR-HOST
ODYTOR_GITHUB_CLIENT_ID=YOUR-OAUTH-APP-ID
ODYTOR_GITHUB_CLIENT_SECRET=YOUR-OAUTH-APP-SECRET
ODYTOR_OAUTH_SIGNING_KEY=YOUR-RANDOM-SECRET-OF-AT-LEAST-32-BYTES
ODYTOR_ALLOWED_GITHUB_USERS=your-github-login
ODYTOR_ALLOWED_REPOSITORIES=owner/repo
```

Once authorized for deployment, run `.venv/bin/odytor --transport streamable-http --host 127.0.0.1 --port 8000` as a dedicated unprivileged service account. Proxy `/mcp`, the OAuth routes and `/.well-known/*` from the public HTTPS origin to that listener, preserving the public Host header and streaming responses. Host and Origin checks allow only the configured public origin. Keep FastMCP's standard encrypted OAuth storage under the service account's private persistent data directory and keep the signing key stable across restarts. There is no Odytor database. Git workspaces use temporary disk and are deleted after each operation; provision disk and execution limits appropriate to the repositories served.

OAuth is provided by FastMCP's standard `GitHubProvider`, including discovery, client registration and consent. Each tool call uses that caller's verified upstream GitHub token and closes its API session afterward. Remote OAuth never falls back to `GITHUB_TOKEN`. It supports the public GitHub API only. Allowed GitHub logins are required; the repository allowlist is optional in OAuth mode.

For compatible clients that supply a static bearer token, `ODYTOR_AUTH_TOKEN` can instead protect a deliberately shared service identity. This mode requires both a configured GitHub token and a nonempty repository allowlist. It does not provide OAuth discovery or per-user GitHub identities; use OAuth for ChatGPT. Do not combine the two configurations.

In ChatGPT, use the custom MCP server connection flow available to your account or workspace, enter `https://YOUR-HOST/mcp`, select OAuth and complete consent, then review the discovered tools. Current connection guidance: [OpenAI custom MCP connections](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt). The implementation follows [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) and [FastMCP GitHub OAuth](https://gofastmcp.com/docs/integrations/github).

Local validation covers Git fixtures, mocked GitHub writes and in-process HTTP authentication and discovery. Real OAuth browser consent, production reverse-proxy behavior and live GitHub mutations have not been exercised. Nothing is deployed by installation or tests.

Odytor complements the [official GitHub MCP server](https://github.com/github/github-mcp-server). That server already exposes general Actions controls and Discussion read/comment/answer operations. This release adds revision-aware coordination instead of duplicating that inventory. Milestone intelligence and additional Discussion writes are deferred until a concrete maintenance workflow justifies them. Conflict inspection plus precise patches and rebase provide the current conflict workflow; automatic conflict resolution is not exposed.

### Optional server-held PAT for one repository (single-user deployment)

For a deployment where the OAuth App can authenticate the operator but does not
have organization-approved access to a repository, a server-held PAT (classic or
fine-grained; GitHub grants them different permissions, and Odytor reports what
GitHub says is missing) may be used **only** for that one repository. This is
deliberately opt-in and requires an exact GitHub login and a nonempty repository
allowlist:

```dotenv
ODYTOR_ALLOWED_GITHUB_USERS=alteixeira20
ODYTOR_ALLOWED_REPOSITORIES=owner/repo,Anvilary/git-plus
ODYTOR_SERVER_PAT_LOGIN=alteixeira20
ODYTOR_SERVER_PAT=YOUR_PRIVATE_PAT
# optional: serve only one repository; leave unset (or `*`) to serve every repository on the allowlist
# ODYTOR_SERVER_PAT_REPOSITORY=owner/repo
```

With no repository set, the PAT is used for that one login on every allowlisted repository (and never for a
repository outside the allowlist); each request's client is still limited to the single repository it targets.

The earlier `ODYTOR_ODYSSEUS_PAT` / `ODYTOR_ODYSSEUS_PAT_LOGIN` variables remain supported as an
alias for `ODYTOR_SERVER_PAT*` with the repository fixed to `odysseus-dev/odysseus`; configuring both
generations at once is refused. Local stdio use needs none of this: set `GITHUB_TOKEN` or `GH_TOKEN`.

Preserve the existing OAuth client ID, secret, signing key and public URL. Git
Plus first verifies the request's OAuth identity; only requests authenticated
as `ODYTOR_SERVER_PAT_LOGIN` and targeting the configured repository
use the PAT. All other allowed repositories continue using each caller's OAuth
token. A missing login or repository allowlist fails server startup.

The PAT-backed client refuses GitHub REST/GraphQL operations targeting other
repositories. Git fetches from contributor forks are anonymous. The one
exception is `quick_fix` on a PR whose head is a contributor fork: after GitHub
confirms the open PR, the fork relationship and maintainer-edit access, Odytor
mints an in-memory grant bound to the authenticated login, upstream repository,
PR number, fork repository, branch, head revision and the `quick_fix`
operation. It allows only a metadata `GET` of that one fork and a single
credentialed Git fast-forward push to that one branch; it expires with the
request. Tool arguments cannot widen it, and redirects, other branches, other
repositories and other operations remain blocked. Applied `pr_rebase` and
workflow dispatch into contributor forks still cannot use the PAT: a rebase
rewrites contributor history, so it is deliberately not extended by this grant.
Read-only conflict inspection may fail if a contributor fork is not publicly
fetchable. CI reruns/approvals for workflows running in the base repository
remain supported when the PAT has the appropriate permissions.

A classic `repo` PAT is inherently broad, even when this application's guards
are correctly implemented. Restrict deployment to the named operator, protect
OAuth storage and the environment file, rotate the token periodically, and
revoke it if compromised. Do not put tokens into Git, images, tool responses,
or client configuration. For additional users or finer authorization, prefer
GitHub App installation credentials over expanding this server-held PAT mode.
