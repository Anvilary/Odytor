# Odytor Security Model

Odytor acts as an operational bridge between AI agents and GitHub repositories. Because AI agents make autonomous decisions, Odytor enforces strict structural boundaries to prevent accidental destruction, credential leakage, and unauthorized modifications.

## Core Security Invariants

1. **Explicit Revision Pinning (TOCTOU Defense)**
   Every mutating operation (`quick_fix`, `pr_rebase`, `ci_control`) and test execution (`test_run`) requires an exact commit SHA (`expected_head`, and `expected_base` where applicable). If a branch advances concurrently between when the agent inspects the pull request and when the mutation is submitted, Odytor refuses the operation.

2. **Repository Allowlists**
   Odytor enforces a strict repository allowlist configured through `ODYTOR_ALLOWED_REPOSITORIES`.
   - **Local stdio mode:** If the allowlist is unset or empty, the local launcher warns that access is unrestricted across repositories visible to the credential. When configured with a comma-separated list of `owner/repo` entries, any attempt to access an unlisted repository fails immediately with a security refusal.
   - **Streamable HTTP / Hosted mode:** The allowlist is mandatory unless operating in authenticated OAuth mode where `ODYTOR_ALLOWED_GITHUB_USERS` restricts access to designated GitHub logins.
   - **Contributor forks:** Access to contributor forks is restricted exclusively to the head repository referenced by an open pull request on an allowlisted base repository.

3. **Branch Leases and Fast-Forward Guarantees**
   When pushing changes to remote branches (such as contributor forks in `quick_fix` or rebases in `pr_rebase`), Odytor employs explicit push leases:
   `--force-with-lease=<ref>:<expected_head>`
   In `quick_fix`, Odytor first proves mathematically in an ephemeral workspace that the newly generated commit is an exact single child of `expected_head`. This ensures that GitHub can only accept the push as a fast-forward append, preserving existing branch history.

4. **Explicit Destructive Operation Guards**
   Potentially dangerous actions cannot be triggered accidentally by default argument values:
   - `pr_rebase` previews changes by default. Applying a rebase requires both `apply=true` and `allow_history_rewrite=true`.
   - `ci_control` dispatch requires `authorize_dispatch_semantics=true` because GitHub Actions dispatches are ref-based and cannot be strictly SHA-pinned.
   - `ci_control` fork workflow approval requires `user_authorized_fork_approval=true`, confirming explicit human approval.

5. **Ephemeral Git Workspaces and No In-Process Code Execution**
   - Git operations run inside temporary directories created with restricted filesystem permissions. Temporary workspaces are purged immediately after each operation completes.
   - Odytor never executes repository build scripts, test suites, pre-commit hooks, or project configuration files inside the main MCP server process.
   - Subprocesses are executed with `core.hooksPath=/dev/null` and disabled credential helpers.

6. **Isolated Test Execution Boundary**
   Test execution (`test_run`) is completely decoupled from the main MCP server:
   - Runs exclusively on a dedicated test runner VM (`odytor-runner`) reached via SSH with a forced command.
   - Tests execute inside rootless Podman containers with:
     - No network access (`--network=none`).
     - Dropped Linux capabilities (`--cap-drop=ALL`).
     - Read-only root filesystem.
     - Unprivileged user namespace (`UID 65534`).
     - Hard memory limits (no swap) and PID limits.
     - Disposable tmpfs filesystems for `/tmp` and `/work`.
   - The test runner never receives GitHub credentials, API keys, or Odytor configuration secrets.

7. **Secret Isolation and Ephemeral Grants**
   - GitHub Personal Access Tokens and OAuth tokens are never written to disk, configuration files, or logs.
   - Subprocesses communicate with GitHub using host-scoped authorization headers rather than embedded URL credentials.
   - **Single-User Server-Held PAT Mode:** When configured with a server-held PAT, Odytor creates an ephemeral in-memory grant for `quick_fix` operations on contributor forks. The grant is scoped strictly to the authenticated caller, the specific open PR, the fork repository, the target branch, and the single commit operation. It expires immediately when the request finishes.

8. **Mutation Uncertainty and Safe Recovery**
   If an HTTP timeout, network interruption, or remote verification ambiguity occurs during a mutation:
   - Odytor reports `outcome: "unknown"`.
   - Mutations are never retried automatically.
   - Callers are expected to fetch the live state of the pull request using `pr_evidence` before deciding on recovery actions.

9. **Untrusted Input Handling**
   All data from external pull requests: including PR descriptions, commit messages, unified diffs, CI job logs, and repository instruction files: is treated as untrusted user input. Odytor bounds payload sizes, validates UTF-8 encoding, and strips or sanitizes dangerous characters before returning responses to AI agent contexts.
