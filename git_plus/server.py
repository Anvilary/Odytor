"""FastMCP server definition and tool registration for Git Plus."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from git_plus.config import Settings
from git_plus.errors import GitPlusError, SecurityError
from git_plus.github import GitHubClient
from git_plus.pr_evidence import get_pr_evidence


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

    return mcp
