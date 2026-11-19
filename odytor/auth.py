"""Authentication setup and request-scoped GitHub client credentials."""

from __future__ import annotations

from contextlib import contextmanager
import hmac
from typing import Iterator
from urllib.parse import urlsplit

from odytor.config import Settings
from odytor.errors import SecurityError
from odytor.github import GitHubClient

def create_auth(settings: Settings, remote: bool = False):
    """Build FastMCP auth for remote mode, or leave local stdio unauthenticated."""
    oauth_values = (
        settings.oauth_client_id,
        settings.oauth_client_secret,
        settings.public_base_url,
        settings.oauth_signing_key,
    )
    if not remote:
        return None

    pat = settings.server_pat_credential()
    if pat:
        if (not all(oauth_values) or settings.auth_token
                or pat.login.casefold() not in {u.casefold() for u in settings.allowed_github_users}
                or not settings.allowed_repositories
                or (pat.repository is not None and pat.repository not in settings.allowed_repositories)):
            raise SecurityError(
                "A server-side PAT requires OAuth, an explicit repository allowlist "
                "and a separately configured PAT owner in the authenticated user allowlist."
            )

    if any(oauth_values):
        if settings.auth_token:
            raise SecurityError("Configure either OAuth or static service authentication, not both.")
        if not all(oauth_values) or not settings.allowed_github_users:
            raise SecurityError("Remote OAuth requires client credentials, public URL, signing key, and allowed GitHub users.")
        if settings.github_api_url != "https://api.github.com":
            raise SecurityError("GitHub OAuth supports only the public GitHub API origin.")
        parsed = urlsplit(settings.public_base_url or "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise SecurityError("Remote OAuth public URL must be an HTTPS URL without credentials, query or fragment.")
        if len((settings.oauth_signing_key or "").encode()) < 32:
            raise SecurityError("OAuth signing key must be at least 32 bytes.")

        from fastmcp.server.auth.providers.github import GitHubProvider

        return GitHubProvider(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            base_url=settings.public_base_url,
            required_scopes=["repo"],
            jwt_signing_key=settings.oauth_signing_key,
        )

    if settings.auth_token and settings.allowed_repositories and settings.github_token:
        from fastmcp.server.auth import StaticTokenVerifier

        return StaticTokenVerifier(
            tokens={settings.auth_token: {"client_id": "odytor-service", "scopes": []}}
        )
    raise SecurityError("Remote mode requires OAuth configuration or an explicit auth token and repository allowlist.")


def caller_principal(settings: Settings, remote: bool = False) -> str:
    """The authenticated caller's identity, applying the same admission rules as github_client, without a GitHub client.

    Used by operations that need an owner for bound state (workspaces, artifacts) or a caller check
    but no GitHub credential. Local stdio is a single trusted principal.
    """
    if not remote:
        return "local"
    from fastmcp.server.dependencies import get_access_token

    access_token = get_access_token()
    if access_token is None:
        raise SecurityError("Authenticated request credentials are required.")
    if settings.oauth_client_id and settings.oauth_client_secret:
        claims = getattr(access_token, "claims", {}) or {}
        login = claims.get("login") if isinstance(claims, dict) else None
        if not isinstance(login, str) or login.casefold() not in {u.casefold() for u in settings.allowed_github_users}:
            raise SecurityError("Authenticated GitHub user is not permitted.")
        return login
    if settings.auth_token and settings.allowed_repositories:
        if access_token.client_id != "odytor-service" or not hmac.compare_digest(access_token.token, settings.auth_token):
            raise SecurityError("Invalid service credentials.")
        return "service"
    raise SecurityError("Remote authentication is not configured.")


@contextmanager
def github_client(settings: Settings, remote: bool = False, *, repo: str | None = None) -> Iterator[GitHubClient]:
    """Create an isolated GitHub client for one call and close its session."""
    token = settings.github_token
    restricted_repo = None
    principal = None
    if remote:
        from fastmcp.server.dependencies import get_access_token

        access_token = get_access_token()
        claims = getattr(access_token, "claims", {}) or {}
        login = claims.get("login") if isinstance(claims, dict) else None
        if access_token is None:
            raise SecurityError("Authenticated request credentials are required.")
        if settings.oauth_client_id and settings.oauth_client_secret:
            if not isinstance(login, str) or login.casefold() not in {u.casefold() for u in settings.allowed_github_users}:
                raise SecurityError("Authenticated GitHub user is not permitted.")
            principal = login
            token = getattr(access_token, "token", None)
            if not token or settings.github_api_url != "https://api.github.com":
                raise SecurityError("Authenticated GitHub credentials are unavailable.")
            pat = settings.server_pat_credential()
            if pat:
                if not repo:
                    raise SecurityError("Repository context is required for server-side PAT selection.")
                target = repo.strip().casefold()
                serves = target == pat.repository if pat.repository else settings.is_repo_allowed(target) and bool(settings.allowed_repositories)
                if serves and login.casefold() == pat.login.casefold():
                    token = pat.token
                    restricted_repo = target
        elif settings.auth_token and settings.allowed_repositories:
            if access_token.client_id != "odytor-service" or not hmac.compare_digest(access_token.token, settings.auth_token):
                raise SecurityError("Invalid service credentials.")
            # Legacy bearer auth deliberately shares one configured GitHub identity.
            token = settings.github_token
        else:
            raise SecurityError("Remote authentication is not configured.")

    kwargs: dict[str, str] = {}
    if restricted_repo:
        kwargs["restricted_repo"] = restricted_repo
    if principal:
        kwargs["principal"] = principal
    client = GitHubClient(token=token, base_url=settings.github_api_url, timeout=settings.github_timeout, **kwargs)
    try:
        yield client
    finally:
        client.session.close()
