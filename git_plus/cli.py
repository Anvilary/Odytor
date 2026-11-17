"""Command-line interface for running the Git Plus MCP server."""

from __future__ import annotations

import argparse
import sys

from git_plus import __version__
from git_plus.config import Settings
from git_plus.server import create_server


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for Git Plus MCP server."""
    parser = argparse.ArgumentParser(
        prog="git-plus",
        description="Git Plus: Minimalist, secure GitHub MCP companion.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"git-plus {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "http"],
        default="stdio",
        help="MCP transport protocol (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address for HTTP/SSE transport (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP/SSE transport (default: 8000).",
    )

    args = parser.parse_args(argv)
    settings = Settings.load()
    server = create_server(settings)

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="sse",
            host=args.host,
            port=args.port,
        )


if __name__ == "__main__":
    main()
