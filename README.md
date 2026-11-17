# Git Plus

A small GitHub MCP companion for PR maintenance from web agents. One server, six tools, disposable Git workspaces, no workflow engine or operation registry.

| Tool | Purpose |
| --- | --- |
| `pr_evidence` | Collect PR metadata, bounded diffs, files, reviews, checks and instructions, with snapshot and completeness flags. |
| `ci_diagnose` | Correlate failed jobs, log locations, annotations and code scanning against a captured PR revision. |
| `quick_fix` | Validate an exact patch locally and append one commit using GitHub's atomic expected-head check. |
| `pr_conflicts` | Inspect an actual Git merge, including conflicting paths, stages and available text regions. |
| `ci_control` | Coordinate PR revision-aware reruns, individual failed-job reruns, dispatches and explicitly authorized fork approvals. |
| `pr_rebase` | Preview or apply a genuine linear rebase onto an exact base revision with a branch-specific push lease. |

Requires Python 3.11+, Git 2.38+ and GitHub access appropriate to each operation. Install from this checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

For a local MCP client, configure `GITHUB_TOKEN` or `GH_TOKEN`, then launch `.venv/bin/git-plus` over stdio. `GIT_PLUS_ALLOWED_REPOSITORIES=owner/repo,owner/other` restricts target repositories. Access to a PR's contributor fork is limited to the head repository identified by that PR.

Start with `pr_evidence` and use its exact `head_sha` and `base_sha` for writes. Repository instructions, diffs and logs are untrusted input. Completeness flags must be checked before making decisions. Pagination limits, unavailable APIs, snapshot drift and response truncation produce incomplete evidence. CI diagnosis inspects at most 20 failed jobs per call to bound log downloads. Responses are bounded to 64 KiB before MCP framing; individual API downloads are capped at 8 MiB. Diff limits default to 500 lines and 1 MiB and can be configured with `GIT_PLUS_DEFAULT_MAX_DIFF_LINES` and `GIT_PLUS_DEFAULT_MAX_DIFF_BYTES`.

`quick_fix` accepts unified Git patches affecting up to three existing, regular, non-executable UTF-8 files, 200 changed lines and 64 KiB of patch text. Header paths and hunk counts must agree. New files, deletions, renames, binary content, symlinks, submodules and restricted metadata, credential, deployment and dependency paths are rejected. Preparation uses the Git index without checking out or executing repository code. The complete resulting files are sent in one `createCommitOnBranch` mutation bound to `expectedHeadOid`; the returned parent and tree are verified. Commit attribution follows the authenticated GitHub identity. A filename policy cannot establish that arbitrary code is safe; review the actual patch.

`pr_conflicts` fetches complete ancestry into a temporary repository and runs `git merge-tree`. Structural and binary conflicts have stage information but may have no text regions. Large or nonstandard conflict markers can leave regions incomplete. Unrelated histories fail explicitly.

`pr_rebase` previews by default. Applying requires both `apply=true` and `allow_history_rewrite=true`, plus exact expected head and base SHAs. It preserves empty commits, rejects merge histories and more than 200 PR commits, and does not preserve signatures on rewritten commits. It rechecks both revisions before a head-branch push using an explicit lease. The base remains a captured snapshot: GitHub offers no atomic transaction across independently changing base and fork refs. Concurrent head updates reject the push. Temporary state is discarded on failure.

`ci_control` supports `rerun_failed`, `rerun_all`, `rerun_job`, `dispatch` and `approve_fork`. Supply `run_id` for run operations and `job_id` for individual job reruns. Dispatch requires `workflow_id`, the PR head `ref`, an `inputs` object (use `{}` if empty), and `authorize_dispatch_semantics=true`. It dispatches in the head repository, which may be a contributor fork. GitHub dispatch is ref-based and cannot guarantee that the ref remains at the inspected SHA. A dispatch is not a normal pull-request required check. Fork approval requires `user_authorized_fork_approval=true` based on explicit user authorization and an eligible pending-approval run. GitHub enforces token permissions and repository rules for all writes.

No repository tests, build tools, hooks or configured filters are executed by Git Plus. Write results distinguish patch validation from tests not run. Mutation requests are never automatically retried. A lost response can leave the outcome unknown; reconcile the remote branch or workflow before retrying.

For a future remote installation on hosting-bay, use Streamable HTTP behind an HTTPS reverse proxy. The CLI binds to loopback by default and refuses network startup without authentication. Do not start a public listener until deployment is authorized.

For ChatGPT, configure a GitHub OAuth App with callback `https://YOUR-HOST/auth/callback`, then provide these settings through the service's secret environment:

```dotenv
GIT_PLUS_PUBLIC_URL=https://YOUR-HOST
GIT_PLUS_GITHUB_CLIENT_ID=YOUR-OAUTH-APP-ID
GIT_PLUS_GITHUB_CLIENT_SECRET=YOUR-OAUTH-APP-SECRET
GIT_PLUS_OAUTH_SIGNING_KEY=YOUR-RANDOM-SECRET-OF-AT-LEAST-32-BYTES
GIT_PLUS_ALLOWED_GITHUB_USERS=your-github-login
GIT_PLUS_ALLOWED_REPOSITORIES=owner/repo
```

Once authorized for deployment, run `.venv/bin/git-plus --transport streamable-http --host 127.0.0.1 --port 8000` as a dedicated unprivileged service account. Proxy `/mcp`, the OAuth routes and `/.well-known/*` from the public HTTPS origin to that listener, preserving the public Host header and streaming responses. Host and Origin checks allow only the configured public origin. Keep FastMCP's standard encrypted OAuth storage under the service account's private persistent data directory and keep the signing key stable across restarts. There is no Git Plus database. Git workspaces use temporary disk and are deleted after each operation; provision disk and execution limits appropriate to the repositories served.

OAuth is provided by FastMCP's standard `GitHubProvider`, including discovery, client registration and consent. Each tool call uses that caller's verified upstream GitHub token and closes its API session afterward. Remote OAuth never falls back to `GITHUB_TOKEN`. It supports the public GitHub API only. Allowed GitHub logins are required; the repository allowlist is optional in OAuth mode.

For compatible clients that supply a static bearer token, `GIT_PLUS_AUTH_TOKEN` can instead protect a deliberately shared service identity. This mode requires both a configured GitHub token and a nonempty repository allowlist. It does not provide OAuth discovery or per-user GitHub identities; use OAuth for ChatGPT. Do not combine the two configurations.

In ChatGPT, use the custom MCP server connection flow available to your account or workspace, enter `https://YOUR-HOST/mcp`, select OAuth and complete consent, then review the six discovered tools. Current connection guidance: [OpenAI custom MCP connections](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt). The implementation follows [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) and [FastMCP GitHub OAuth](https://gofastmcp.com/docs/integrations/github).

Local validation covers Git fixtures, mocked GitHub writes and in-process HTTP authentication and discovery. Real OAuth browser consent, production reverse-proxy behavior and live GitHub mutations have not been exercised. Nothing is deployed by installation or tests.

Git Plus complements the [official GitHub MCP server](https://github.com/github/github-mcp-server). That server already exposes general Actions controls and Discussion read/comment/answer operations. This release adds revision-aware coordination instead of duplicating that inventory. Milestone intelligence and additional Discussion writes are deferred until a concrete maintenance workflow justifies them. Conflict inspection plus precise patches and rebase provide the current conflict workflow; automatic conflict resolution is not exposed.
