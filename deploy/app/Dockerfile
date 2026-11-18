# Minimal production image for the Odytor MCP server.
#
# The container user, its home (/home/git-plus) and the data paths below are legacy identifiers of the existing
# deployment: its .env pins HOME and FASTMCP_HOME there and its volumes are mounted there. They are not branding.
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 git-plus && \
    useradd -u 10001 -g git-plus -m -d /home/git-plus -s /bin/bash git-plus

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml README.md ./
COPY odytor/ ./odytor/

RUN uv pip install --system --no-cache "."

RUN mkdir -p /home/git-plus/data/.fastmcp /home/git-plus/test-data && \
    chown -R git-plus:git-plus /home/git-plus && \
    chmod 700 /home/git-plus/data /home/git-plus/data/.fastmcp /home/git-plus/test-data

USER git-plus
WORKDIR /home/git-plus

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/home/git-plus/.local/bin:${PATH}" \
    HOME="/home/git-plus" \
    FASTMCP_HOME="/home/git-plus/data/.fastmcp"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS -H "Host: git-plus.alexandreteixeira.dev" http://127.0.0.1:8000/.well-known/oauth-authorization-server || exit 1

ENTRYPOINT ["odytor"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
