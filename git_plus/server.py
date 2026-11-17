"""FastMCP server definition and tool registration for Git Plus."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from git_plus.ci_diagnose import diagnose_ci
from git_plus.config import Settings
from git_plus.errors import GitPlusError, SecurityError
from git_plus.github import GitHubClient
from git_plus.pr_evidence import get_pr_evidence
from git_plus.quick_fix import apply_quick_fix
from git_plus.pr_conflicts import get_pr_conflicts
from git_plus.pr_rebase import rebase_pr


def create_server(settings: Settings | None = None) -> FastMCP:
    """Create and configure the Git Plus FastMCP server instance."""
    cfg = settings or Settings.load()
    client = GitHubClient(token=cfg.github_token, base_url=cfg.github_api_url)

    mcp = FastMCP(
        "Git Plus",
        instructions="Minimalist, secure GitHub MCP companion optimized for web-based AI agents.",
    )

    @mcp.tool(
        name="pr_evidence",
        description=(
            "Retrieve comprehensive, snapshot-consistent pull request evidence in a single call. "
            "Includes PR metadata, exact head/base revisions, changed files, unified diffs, "
            "reviews, review threads, check status, and repository instructions. "
            "Provides explicit completeness metadata and detects snapshot drift."
        ),
    )
    def pr_evidence(
        repo: Annotated[
            str,
            Field(description="Target repository in 'owner/repo' format (e.g. 'octocat/Hello-World')"),
        ],
        number: Annotated[
            int,
            Field(description="Pull request number (positive integer)", ge=1),
        ],
        expected_head: Annotated[
            str | None,
            Field(description="Optional 40-character commit SHA to verify PR head has not moved"),
        ] = None,
        include_diff: Annotated[
            bool,
            Field(description="Whether to include unified diff text"),
        ] = True,
        max_diff_lines: Annotated[
            int,
            Field(description="Maximum unified diff lines to return (1-5000)", ge=1, le=5000),
        ] = 500,
        include_files: Annotated[
            bool,
            Field(description="Whether to include list of changed files"),
        ] = True,
        include_reviews: Annotated[
            bool,
            Field(description="Whether to include reviews and review threads"),
        ] = True,
        include_checks: Annotated[
            bool,
            Field(description="Whether to include check runs and status rollup"),
        ] = True,
        include_instructions: Annotated[
            bool,
            Field(description="Whether to fetch repository instructions or PR template"),
        ] = True,
        files_page: Annotated[
            int,
            Field(description="Page number for changed files pagination", ge=1),
        ] = 1,
        files_per_page: Annotated[
            int,
            Field(description="Page size for changed files (1-100)", ge=1, le=100),
        ] = 100,
    ) -> dict[str, Any]:
        """Fetch unified PR evidence with completeness verification."""
        if not cfg.is_repo_allowed(repo):
            raise SecurityError(f"Repository '{repo}' is not permitted by Git Plus configuration.")

        try:
            return get_pr_evidence(
                client,
                repo,
                number,
                expected_head=expected_head,
                include_diff=include_diff,
                max_diff_lines=max_diff_lines,
                include_files=include_files,
                include_reviews=include_reviews,
                include_checks=include_checks,
                include_instructions=include_instructions,
                files_page=files_page,
                files_per_page=files_per_page,
            )
        except GitPlusError as err:
            return err.to_dict()

    @mcp.tool(
        name="ci_diagnose",
        description=(
            "Diagnose CI failures for a pull request. Collects failed workflow jobs, "
            "extracts log excerpts with file/line references, fetches check annotations, "
            "and optionally correlates code scanning alerts. Differentiates infra failures "
            "from test, lint, type-check, and compiler errors. Requires exact head SHA for "
            "revision consistency. Use pr_evidence first to obtain head_sha."
        ),
    )
    def ci_diagnose(
        repo: Annotated[
            str,
            Field(description="Target repository in 'owner/repo' format"),
        ],
        number: Annotated[
            int,
            Field(description="Pull request number (positive integer)", ge=1),
        ],
        expected_head: Annotated[
            str | None,
            Field(description="Optional 40-character commit SHA to verify PR head has not moved"),
        ] = None,
        run_id: Annotated[
            int | None,
            Field(description="Specific Actions run ID to diagnose; null inspects all runs for the PR head"),
        ] = None,
        job_id: Annotated[
            int | None,
            Field(description="Specific Actions job ID to diagnose within the selected run"),
        ] = None,
        max_excerpts_per_job: Annotated[
            int,
            Field(description="Maximum log failure excerpts returned per job (1-100)", ge=1, le=100),
        ] = 30,
        include_annotations: Annotated[
            bool,
            Field(description="Whether to fetch check run annotations per job"),
        ] = True,
        include_code_scanning: Annotated[
            bool,
            Field(description="Whether to fetch code scanning alerts for the PR head"),
        ] = True,
    ) -> dict[str, Any]:
        """Collect actionable CI failure evidence for a pull request."""
        if not cfg.is_repo_allowed(repo):
            raise SecurityError(f"Repository '{repo}' is not permitted by Git Plus configuration.")

        try:
            return diagnose_ci(
                client,
                repo,
                number,
                expected_head=expected_head,
                run_id=run_id,
                job_id=job_id,
                max_excerpts_per_job=max_excerpts_per_job,
                include_annotations=include_annotations,
                include_code_scanning=include_code_scanning,
            )
        except GitPlusError as err:
            return err.to_dict()

    @mcp.tool(name="quick_fix", description="Apply an exact bounded unified diff as one additive commit to an open PR. Requires current head SHA. Validates patch and file modes; does not run tests. No force push.")
    def quick_fix(repo: str, number: Annotated[int, Field(ge=1)], expected_head: str,
                  patch: str, commit_message: str) -> dict[str, Any]:
        if not cfg.is_repo_allowed(repo):
            raise SecurityError(f"Repository '{repo}' is not permitted by Git Plus configuration.")
        try:
            return apply_quick_fix(client, repo, number, expected_head, patch, commit_message)
        except GitPlusError as err:
            return err.to_dict()

    @mcp.tool(name="pr_conflicts", description="Inspect an exact head/base merge using isolated Git. Returns conflicting paths, index stages and actual text regions with completeness metadata. Executes no repository code.")
    def pr_conflicts(repo: str, number: Annotated[int, Field(ge=1)], expected_head: str,
                     expected_base: str | None = None, max_paths: Annotated[int, Field(ge=1, le=100)] = 100) -> dict[str, Any]:
        if not cfg.is_repo_allowed(repo):
            raise SecurityError(f"Repository '{repo}' is not permitted by Git Plus configuration.")
        try:
            return get_pr_conflicts(client, repo, number, expected_head, expected_base, max_paths)
        except GitPlusError as err:
            return err.to_dict()

    @mcp.tool(name="pr_rebase", description="Preview a genuine rebase of a linear PR onto an exact base revision. Applying rewrites history and requires apply=true plus allow_history_rewrite=true. Pushes with an exact branch lease. Does not run repository tests.")
    def pr_rebase(repo: str, number: Annotated[int, Field(ge=1)], expected_head: str, expected_base: str,
                  apply: bool = False, allow_history_rewrite: bool = False) -> dict[str, Any]:
        if not cfg.is_repo_allowed(repo):
            raise SecurityError(f"Repository '{repo}' is not permitted by Git Plus configuration.")
        try:
            return rebase_pr(client, repo, number, expected_head, expected_base,
                             apply=apply, allow_history_rewrite=allow_history_rewrite)
        except GitPlusError as err:
            return err.to_dict()

    return mcp
