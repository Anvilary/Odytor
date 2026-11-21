"""Command-line interface for running the Odytor MCP server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from odytor import __version__
from odytor.config import Settings
from odytor.envvars import legacy_names_in_use
from odytor.server import create_server


def startup_notes(settings: Settings) -> list[str]:
    """Plain-language notes for a local (stdio) start. They go to stderr; stdout belongs to the MCP protocol."""
    notes = []
    legacy = legacy_names_in_use()
    if legacy:
        notes.append(f"{len(legacy)} setting(s) use the temporary GIT_PLUS_ prefix ({', '.join(legacy[:3])}"
                     f"{', ...' if len(legacy) > 3 else ''}); rename them to ODYTOR_.")
    if not settings.github_token:
        notes.append("no GitHub credential: set GITHUB_TOKEN or GH_TOKEN. Only public data can be read and writes will fail.")
    if not settings.allowed_repositories:
        notes.append("ODYTOR_ALLOWED_REPOSITORIES is not set: every repository the credential can reach is permitted. "
                     "Set it to owner/repo,owner/other to restrict access.")
    if settings.testing is None:
        notes.append("isolated testing is not configured: test_run and test_result will explain how to enable it "
                     "(the other six tools are unaffected).")
    return notes


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for the Odytor MCP server."""
    parser = argparse.ArgumentParser(
        prog="odytor",
        description="Odytor: GitHub operations, optimized for AI agents.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"odytor {__version__}",
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

    parser.add_argument("--drain", choices=["start", "status", "resume"],
                        help="Control execution admission on this instance's configured persistent data volume.")
    args = parser.parse_args(argv)
    settings = Settings.load()
    if args.drain:
        if settings.testing is None:
            parser.error("--drain requires an isolated runner configuration")
        from odytor.jobs import execution_drain
        print(json.dumps(execution_drain(Path(settings.testing.data_dir), args.drain)))
        return
    if args.transport == "stdio":
        for note in startup_notes(settings):
            print(f"odytor: {note}", file=sys.stderr)
        server = create_server(settings)
        server.run(transport="stdio")
    else:
        server = create_server(settings, remote=True)
        public = urlsplit(settings.public_base_url or f"http://{args.host}:{args.port}")
        server.run(
            host_origin_protection=True,
            allowed_hosts=[public.netloc],
            allowed_origins=[f"{public.scheme}://{public.netloc}"],
            transport="sse" if args.transport == "sse" else "streamable-http",
            host=args.host,
            port=args.port,
            **({"stateless_http": True} if args.transport in {"http", "streamable-http"} else {}),
        )


if __name__ == "__main__":
    main()
