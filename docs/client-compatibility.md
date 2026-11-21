# MCP client compatibility, approvals and authentication

Status words are exact: **PASS** (tested), **NOT TESTED** (no access from the development environment), **DOCUMENTED**
(taken from third-party documentation, not verified here).

## What was verified

| Check | Result |
|---|---|
| FastMCP in-process client: tool listing, schemas, annotations, all new tools | PASS (`tests/test_workspace_tools.py`, `tests/test_browser_e2e.py`) |
| Streamable HTTP transport with authentication, tool discovery | PASS (`tests/test_auth.py`) |
| Screenshots arrive as native MCP `image` content blocks (PNG, valid signature), not as base64 in the JSON text | PASS (real Chromium, real MCP client) |
| Same screenshots retrievable again by artifact id; traces retrievable in bounded base64 chunks | PASS |
| Claude Code (this session's MCP client) displaying an Odytor image block | **NOT TESTED** (the Odytor MCP server attached to this session is an older build; the new tools were exercised through the FastMCP client) |
| ChatGPT, Perplexity, Codex displaying native image content | **NOT TESTED** |

If a client does not render image content, `artifact_get(mode="base64")` is the bounded, authenticated fallback; no
public URL is ever created for private artifacts.

## Why some clients ask for approval on every test submission

Four layers can cause a prompt; Odytor controls only the first.

1. **Tool annotations (server).** `test_run`, `workspace_run` and `browser_run` consume resources and are marked
   `readOnlyHint: false`. They are **not** relabelled read-only: that would misstate what they do, and clients are right
   to treat unannotated or non-read-only tools as needing consent. Pure reads (`workspace_read`, `workspace_search`,
   `workspace_diff`, `artifact_get`, `odytor_capabilities`, `test_result`) are read-only and need no consent in clients
   that honor the hint.
2. **Client policy.** Clients decide independently what to prompt for. This is client behavior and was not changed or
   bypassed.
3. **Connector behavior.** Some connectors prompt per call regardless of hints. **NOT TESTED** per client.
4. **Server-side authorization.** Odytor has no per-call interactive approval, so it cannot add prompts or remove them.

### What Odytor does to reduce friction safely

* **Fewer calls that need consent.** A whole browser session or test cycle is one non-read-only call plus read-only
  polling; reads, search and diff cost no consent in compliant clients.
* **Bounded authorization is already in the workspace.** A workspace carries `scope = {operations, policy_version}`,
  is bound to the caller, repository and lifetime, and has caps on size, files, count and runs. An operator or user can
  create a workspace with only `read`, `search`, `edit`, `diff`, `execute`, `artifacts`, `manage`; the client's consent
  to *creating* that workspace is the meaningful decision, after which iterative scratch work happens inside it.
* **External writes stay separate.** `publish` is not in the default scope, and `publish_apply` needs
  `user_authorized=true` plus a plan-bound confirmation. No amount of scratch-work consent grants a GitHub write.

When the host supports per-tool allowlists, allow the connector's exact names for `workspace_create`,
`workspace_edit`, `workspace_run`, `test_run`, `browser_run`, and `validation_campaign` if you want routine
disposable preparation to proceed without repeated prompts. `publish_plan` and `pr_body_plan` also prepare
local plans without writing to GitHub. Keep `publish_apply`, `pr_body_apply`, `quick_fix`, `pr_rebase`, and
`ci_control` outside that allowlist so the host prompts for the exact consequential call. Hosts that require
approval for every state-changing tool can use a single `validation_campaign` submission followed by read-only
`campaign_status`, `test_result`, and `artifact_get` calls.

No approval service was built: the client behavior in layers 2 and 3 is what actually produces the prompts, and a
server-side service cannot change it.

## Authentication

Two independent concerns:

* **GitHub API credentials.** The owner's preference, a server-held classic PAT, is preserved. It is selected only for
  the PAT's exact GitHub login and allowlisted repositories, scoped to one repository per request, and never sent to
  MCP clients, runner containers, browsers, fixtures, logs or tool responses (the runner environment is built from an
  explicit allowlist; the runner host holds no GitHub credentials).
* **Client-to-Odytor authentication.** Today Odytor supports GitHub OAuth with a user allowlist (required when a
  server-held PAT is used) or a static bearer token for a single shared identity. Neither was weakened.

**DOCUMENTED, not verified:** third-party guides report that ChatGPT's custom connector form offers OAuth or no
authentication and has no field for a bearer token, while Perplexity's MCP support is described only through its own API
keys. If a client can use neither GitHub OAuth nor a bearer header, the safe options are an OAuth-capable front (Cloudflare
Access or a dedicated OAuth proxy in front of the private endpoint) or a different client; **an unauthenticated public
endpoint is not an acceptable option** for an instance that can run code and hold a PAT. Choosing between those is an
operator decision and was not changed here. Sources consulted:
[ChatGPT connector setup (Zuplo)](https://zuplo.com/docs/mcp-gateway/connect-clients/chatgpt.md),
[Adding a remote MCP server to ChatGPT (Auth0)](https://auth0.com/blog/add-remote-mcp-server-chatgpt/).
