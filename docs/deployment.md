# Odytor Deployment and Integration Guide

Odytor can be operated in two primary modes:
1. **Local stdio mode:** For desktop agent CLIs (Claude Code, Codex, Antigravity CLI, Agent Hub).
2. **Hosted Streamable HTTP mode:** For web-based agents and services (ChatGPT custom MCP server, automated pipelines).

---

## Local Stdio Deployment

Local stdio mode is the simplest way to run Odytor alongside desktop coding assistants.

### Requirements
- Python 3.11 or newer
- Git 2.38 or newer
- GitHub CLI (`gh`) logged in with appropriate repository scopes (`repo`, `workflow`)

### Quick Setup

1. **Clone and install:**
   ```bash
   git clone https://github.com/Anvilary/Odytor.git
   cd Odytor
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```

2. **Verify CLI installation:**
   ```bash
   .venv/bin/odytor --version
   ```

3. **Configure local launcher:**
   A production-ready launcher is provided in `deploy/local/odytor-mcp`. It automatically extracts the caller's session token from the GitHub CLI without storing secrets on disk:
   ```bash
   cp deploy/local/odytor-mcp ~/.local/bin/anvi-odytor-mcp
   chmod +x ~/.local/bin/anvi-odytor-mcp
   ```

4. **Register with Agent Hub or MCP Clients:**

   **Claude Code configuration (`~/.claude.json`):**
   ```json
   {
     "mcpServers": {
       "anvi-odytor": {
         "command": "/home/YOUR_USER/.local/bin/anvi-odytor-mcp"
       }
     }
   }
   ```

   **Direct stdio launch without launcher:**
   ```bash
   export GITHUB_TOKEN="ghp_your_personal_access_token"
   export ODYTOR_ALLOWED_REPOSITORIES="owner/repo,owner/other"
   .venv/bin/odytor --transport stdio
   ```

---

## Hosted Streamable HTTP Deployment

Hosted deployment exposes the Model Context Protocol over HTTP using the Streamable HTTP transport specification. This allows clients like ChatGPT to connect remotely.

### Architecture

```text
[Internet Client / ChatGPT]
            |
            v  HTTPS (TLS termination)
   [Nginx / Cloudflare Tunnel]
            |
            v  Streamable HTTP (localhost:8000)
    [Odytor Server Container]
            |
            +--> GitHub REST & GraphQL API
            |
            +--> [SSH with forced command] --> [Isolated Runner VM]
```

### GitHub OAuth Setup for ChatGPT

1. Register or update your GitHub OAuth App:
   - **Application Name:** Odytor MCP Server
   - **Homepage URL:** `https://odytor.alexandreteixeira.dev`
   - **Authorization callback URL:** `https://odytor.alexandreteixeira.dev/auth/callback`

2. Configure environment variables in `.env`:
   ```dotenv
   ODYTOR_PUBLIC_URL=https://odytor.alexandreteixeira.dev
   ODYTOR_GITHUB_CLIENT_ID=your_github_client_id
   ODYTOR_GITHUB_CLIENT_SECRET=your_github_client_secret
   ODYTOR_OAUTH_SIGNING_KEY=generate_random_secret_at_least_32_bytes
   ODYTOR_ALLOWED_GITHUB_USERS=authorized_username
   ODYTOR_ALLOWED_REPOSITORIES=owner/repo1,owner/repo2
   ```

3. Run Odytor under a dedicated, unprivileged system user or container:
   ```bash
   .venv/bin/odytor --transport streamable-http --host 127.0.0.1 --port 8000
   ```

4. Configure your reverse proxy (Nginx or Cloudflare Tunnel) to forward:
   - `/mcp` (must preserve streaming responses and disable buffering: `proxy_buffering off`)
   - `/auth/*` (OAuth flow routes)
   - `/.well-known/*` (OAuth discovery endpoints)

5. In ChatGPT:
   - Go to Settings -> Connected apps or Custom MCP servers.
   - Enter `https://odytor.alexandreteixeira.dev/mcp`.
   - Select OAuth authentication and complete consent.
   - Enter `https://your-domain.example.com/mcp`.
   - Select OAuth authentication and complete consent.

---

## Authentication Modes

| Mode | Target Use Case | Credential Handling | Accepted Token Scopes |
| --- | --- | --- | --- |
| **Local Stdio PAT** | Developer workstations, desktop agent CLIs | Token passed via `GITHUB_TOKEN` from `gh auth token` or shell environment | `repo`, `workflow` |
| **Hosted GitHub OAuth** | Remote multi-user or ChatGPT connections | FastMCP OAuth provider; caller signs in with their GitHub account | Caller-granted OAuth scopes |
| **Server-Held PAT** | Single-operator hosted deployment without full org OAuth grants | Token held in server environment; activated only for allowlisted user | `repo`, `workflow` |
| **Static Bearer Token** | Private service-to-service integration | Protected by `ODYTOR_AUTH_TOKEN` header | Uses server `GITHUB_TOKEN` |

### Server-Held PAT Mode (Single-User Hosted)

When an OAuth app cannot receive organization permissions for private repositories, Odytor can use a server-held PAT strictly for a specific operator login:

```dotenv
ODYTOR_ALLOWED_GITHUB_USERS=alice
ODYTOR_ALLOWED_REPOSITORIES=my-org/my-repo
ODYTOR_SERVER_PAT_LOGIN=alice
ODYTOR_SERVER_PAT=ghp_secret_operator_pat
# Optional: restrict PAT to a single repository
ODYTOR_SERVER_PAT_REPOSITORY=my-org/my-repo
```

Requests authenticated as `alice` access the repository using the PAT. Requests from other users or targeting other repositories are rejected.

---

## Configuration Reference

All settings can be configured via environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ODYTOR_ALLOWED_REPOSITORIES` | `""` | Comma-separated list of `owner/repo` targets permitted |
| `ODYTOR_MAX_RESPONSE_BYTES` | `262144` (256 KiB) | Maximum MCP response payload before field truncation (16 KiB to 4 MiB) |
| `ODYTOR_GITHUB_TIMEOUT` | `120` | Read timeout in seconds for GitHub API calls (10 to 600) |
| `ODYTOR_DEFAULT_MAX_DIFF_LINES` | `500` | Default diff window line limit |
| `ODYTOR_DEFAULT_MAX_DIFF_BYTES` | `1048576` (1 MiB) | Default diff window byte limit |
| `ODYTOR_QUICK_FIX_MAX_FILES` | `10` | Maximum number of files permitted in a single quick fix patch (up to 50) |
| `ODYTOR_QUICK_FIX_MAX_CHANGED_LINES`| `1000` | Maximum changed lines permitted in a quick fix patch (up to 5000) |
| `ODYTOR_QUICK_FIX_MAX_PATCH_BYTES` | `262144` (256 KiB) | Maximum patch file size for quick fix (up to 1 MiB) |
| `ODYTOR_PUBLIC_URL` | `""` | Canonical external HTTPS URL for OAuth discovery and callbacks |
| `ODYTOR_ALLOWED_GITHUB_USERS` | `""` | Comma-separated list of allowed GitHub logins for OAuth mode |
| `ODYTOR_AUTH_TOKEN` | `""` | Static bearer token secret for non-OAuth HTTP deployments |
| `ODYTOR_TEST_RUNNER_COMMAND` | `""` | JSON argv array for invoking the isolated test runner |
| `ODYTOR_TEST_POLICIES` | `""` | Path to repository test policies TOML file |
| `ODYTOR_TEST_DATA_DIR` | `~/.local/state/odytor/tests` | Spool directory for retained test results |
