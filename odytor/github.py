"""GitHub REST and GraphQL API client for Odytor."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin
from typing import Any

import requests

from odytor.diagnostics import safe_payload, safe_text
from odytor import __version__
from odytor.errors import APIError, SecurityError, ValidationError

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEX_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


def validate_repo(repo: str) -> tuple[str, str]:
    """Validate repository format 'owner/repo' and return (owner, repo)."""
    cleaned = repo.strip()
    if not _REPO_PATTERN.fullmatch(cleaned):
        raise ValidationError(
            f"Invalid repository format '{repo}'. Expected 'owner/repo'."
        )
    owner, name = cleaned.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise SecurityError(f"Prohibited repository path '{repo}'.")
    return owner, name


def validate_sha(sha: str, name: str = "commit SHA") -> str:
    """Validate 40-character hex commit SHA."""
    cleaned = sha.strip()
    if not _HEX_SHA_PATTERN.fullmatch(cleaned):
        raise ValidationError(
            f"Invalid {name} '{sha}'. Expected exact 40-character hexadecimal SHA."
        )
    return cleaned.lower()


FORK_WRITE_OPERATIONS = frozenset({"quick_fix"})


@dataclass(frozen=True)
class ForkWriteGrant:
    """Narrow authority for one PR head branch revision in a contributor fork.

    It is minted only from GitHub's own PR response, never from tool arguments,
    and is bound to the authenticated user, upstream repository, PR number, head
    repository, branch, revision and a single operation.
    """

    principal: str
    base_repo: str
    number: int
    head_repo: str
    head_ref: str
    head_sha: str
    operation: str


USER_AGENT = f"Odytor-MCP/{__version__}"


class GitHubClient:
    """Stateless GitHub API client with automatic retry, pagination, and redaction."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout: float = 120.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        max_response_bytes: int = 8 * 1024 * 1024,
        restricted_repo: str | None = None,
        principal: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SecurityError("GitHub API URL must be HTTPS without credentials, query or fragment.")
        if timeout <= 0 or max_retries < 1 or max_response_bytes <= 0:
            raise ValidationError("Timeout and attempt count must be positive.")
        self.token = token
        self.base_url = base_url.rstrip("/")
        # (connect, read): GitHub can take a long time to build the diff or file list of a very large PR.
        self.timeout = (min(10.0, timeout), timeout)
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.auth = None
        if restricted_repo is not None:
            validate_repo(restricted_repo)
        self.restricted_repo = restricted_repo.casefold() if restricted_repo else None
        self.principal = principal.strip().casefold() if principal and principal.strip() else None
        self.fork_grant: ForkWriteGrant | None = None

    def authorize_fork_write(self, grant: ForkWriteGrant) -> None:
        """Let a scoped PAT touch exactly one verified contributor branch."""
        if not self.restricted_repo:
            raise SecurityError("Fork write grants apply only to scoped credentials.")
        validate_repo(grant.head_repo)
        validate_sha(grant.head_sha)
        if (not self.principal or grant.principal != self.principal
                or grant.base_repo.casefold() != self.restricted_repo
                or grant.head_repo.casefold() == self.restricted_repo
                or grant.operation not in FORK_WRITE_OPERATIONS
                or type(grant.number) is not int or grant.number <= 0 or not grant.head_ref):
            raise SecurityError("Fork write grant does not match the authenticated scope.")
        self.fork_grant = grant

    def revoke_fork_write(self) -> None:
        self.fork_grant = None

    def covers_fork_write(self, repo: str, ref: str, sha: str, operation: str) -> bool:
        grant = self.fork_grant
        return bool(grant and grant.head_repo.casefold() == repo.casefold() and grant.head_ref == ref
                    and grant.head_sha == sha and grant.operation == operation)

    def require_repository(self, repo: str) -> None:
        """Prevent a privileged PAT from reaching unrelated GitHub repositories."""
        validate_repo(repo)
        if self.restricted_repo and repo.casefold() != self.restricted_repo:
            raise SecurityError("Server-side PAT cannot access another GitHub repository.")

    def _check_scoped_request(self, path: str, json_data: Any, method: str = "GET") -> None:
        if not self.restricted_repo:
            return
        parts = urlsplit(path).path.lstrip("/").split("/")
        if len(parts) >= 3 and parts[0] == "repos":
            grant = self.fork_grant
            if (grant and len(parts) == 3 and not urlsplit(path).query
                    and f"{parts[1]}/{parts[2]}".casefold() == grant.head_repo.casefold()):
                if method != "GET":
                    raise SecurityError("Contributor fork API access is read-only metadata.")
                return
            self.require_repository(f"{parts[1]}/{parts[2]}")
            return
        if parts == ["graphql"] and isinstance(json_data, dict):
            query = json_data.get("query", "")
            vars = json_data.get("variables") or {}
            if not isinstance(query, str) or not isinstance(vars, dict):
                raise SecurityError("Unrecognized scoped GraphQL request.")
            if query.lstrip().startswith("query(") and "reviewThreads(" in query:
                owner, name = vars.get("owner"), vars.get("repo")
                if isinstance(owner, str) and isinstance(name, str):
                    self.require_repository(f"{owner}/{name}")
                    return
            if query.lstrip().startswith("mutation(") and "createCommitOnBranch(" in query:
                branch = (vars.get("input") or {}).get("branch") or {}
                target = branch.get("repositoryNameWithOwner") if isinstance(branch, dict) else None
                if isinstance(target, str):
                    self.require_repository(target)
                    return
        raise SecurityError("Server-side PAT request is outside its supported repository scope.")

    def _credential_diagnostic(self, resp: requests.Response) -> dict[str, Any]:
        """Say what GitHub told us about the credential without claiming more than it did.

        Classic and fine-grained PATs, OAuth tokens and server-side PATs authorize differently; GitHub reports
        the permissions an endpoint accepts (`X-Accepted-GitHub-Permissions`) and the scopes of classic and OAuth
        tokens (`X-OAuth-Scopes`), so those are passed on verbatim and truncated.
        """
        status = resp.status_code
        if status not in {401, 403, 404}:
            return {}
        if not self.token:
            return {"classification": "no_credential_configured",
                    "hint": "No GitHub credential is configured; set GITHUB_TOKEN or GH_TOKEN (or authenticate through OAuth)."}
        info: dict[str, Any] = {}
        accepted = resp.headers.get("x-accepted-github-permissions")
        granted = resp.headers.get("x-oauth-scopes")
        if accepted:
            info["accepted_permissions"] = safe_text(accepted, 200)
        if granted is not None:
            info["token_scopes"] = safe_text(granted, 200)
        if status == 401:
            return {**info, "classification": "bad_credentials",
                    "hint": "GitHub rejected the credential: it may be expired, revoked or malformed."}
        if status == 403:
            return {**info, "classification": "insufficient_permissions",
                    "hint": "The credential is valid but lacks a permission or scope this operation needs, or the "
                            "organization has not approved it. Compare accepted_permissions with the token's grants."}
        return {**info, "classification": "not_found_or_no_access",
                "hint": "GitHub answers 404 both for missing resources and for repositories this credential cannot see."}

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        data: Any = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> requests.Response:
        """Execute a GitHub API request with bounded retry and rate limit checks."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc or urlsplit(url).scheme != "https":
            raise SecurityError("API requests must stay on the configured GitHub origin.")
        self._check_scoped_request(url, json_data, method.upper())
        headers = self._headers(accept=accept)
        attempts = self.max_retries if method.upper() in {"GET", "HEAD"} else 1

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._bounded_request(method, url, params=params, json=json_data,
                                             data=data, headers=headers)

                if resp.status_code in {502, 503, 504} and attempt < attempts - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue

                if resp.status_code == 429 or (resp.status_code == 403 and (resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower())):
                    reset_time = resp.headers.get("x-ratelimit-reset", "unknown")
                    raise APIError(
                        f"GitHub API rate limit exceeded. Reset at epoch timestamp: {reset_time}",
                        status_code=resp.status_code,
                        diagnostic={"classification": "rate_limit_exceeded", "reset": reset_time, "retry_after": resp.headers.get("retry-after")},
                    )

                if not resp.ok:
                    diagnostic = {
                        "status_code": resp.status_code,
                        "url": path,
                        "method": method,
                        "message": safe_text(resp.text.replace(self.token, "[REDACTED]") if self.token else resp.text, 2048),
                        **self._credential_diagnostic(resp),
                    }
                    raise APIError(
                        f"GitHub API request failed with status {resp.status_code}.",
                        status_code=resp.status_code,
                        diagnostic=diagnostic,
                    )

                return resp

            except requests.RequestException as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                break

        msg = safe_text(str(last_error), 512) if last_error else "Network error"
        raise APIError(
            f"GitHub request connection failed: {msg}",
            diagnostic={"classification": "network_error", "message": msg},
        )

    def _bounded_request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Follow only same-origin API redirects or credential-free Actions log downloads."""
        initial = urlsplit(url)
        for hop in range(4):
            response = self.session.request(method=method, url=url, timeout=self.timeout,
                                            allow_redirects=False, stream=True, **kwargs)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                target = urlsplit(urljoin(url, location))
                same_origin = target.scheme == initial.scheme and target.netloc == initial.netloc
                log_download = (method.upper() == "GET" and initial.path.endswith("/logs")
                                and target.scheme == "https" and target.hostname is not None
                                and (target.hostname.endswith(".blob.core.windows.net")
                                     or target.hostname.endswith(".actions.githubusercontent.com")))
                if target.username or target.password or not (same_origin or log_download):
                    raise SecurityError("GitHub redirect left the approved API or Actions log origins.")
                if same_origin:
                    # Redirects must not move a scoped PAT into another repository.
                    self._check_scoped_request(target.geturl(), kwargs.get("json"), method.upper())
                if method.upper() not in {"GET", "HEAD"}:
                    raise APIError("Mutation redirect refused; resolve the canonical repository first.")
                # Never forward API credentials or parameters to signed log URLs.
                if not same_origin:
                    kwargs = {"headers": {"User-Agent": USER_AGENT}}
                url = target.geturl()
                continue
            try:
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise APIError("GitHub response exceeds the configured byte limit.", diagnostic={
                            "classification": "response_too_large", "max_bytes": self.max_response_bytes,
                        })
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                return response
            finally:
                response.close()
        raise APIError("Too many GitHub redirects.")

    def get_pr(self, repo: str, number: int) -> dict[str, Any]:
        """Fetch pull request metadata."""
        validate_repo(repo)
        resp = self.request("GET", f"repos/{repo}/pulls/{number}")
        return resp.json()

    @staticmethod
    def is_diff_too_large(exc: APIError) -> bool:
        """GitHub refuses the diff of a PR over 20,000 lines or 300 files with 406; the files API still works."""
        message = str((exc.diagnostic or {}).get("message", "")).lower()
        return exc.status_code == 406 and "diff exceeded the maximum" in message

    def get_pr_diff(self, repo: str, number: int) -> str:
        """Fetch unified diff text for a pull request."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
        )
        return resp.text

    def get_pr_files(
        self,
        repo: str,
        number: int,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch changed files page for a pull request."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/pulls/{number}/files",
            params={"per_page": per_page, "page": page},
        )
        return resp.json()

    def get_pr_reviews(
        self,
        repo: str,
        number: int,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch reviews page for a pull request."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/pulls/{number}/reviews",
            params={"per_page": per_page, "page": page},
        )
        return resp.json()

    def get_pr_comments(
        self,
        repo: str,
        number: int,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch PR issue comments page."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/issues/{number}/comments",
            params={"per_page": per_page, "page": page},
        )
        return resp.json()

    def get_check_runs(self, repo: str, ref: str) -> dict[str, Any]:
        """Fetch check runs for a commit ref."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/commits/{ref}/check-runs",
            params={"per_page": 100},
        )
        return resp.json()

    def get_workflow_runs(
        self,
        repo: str,
        *,
        head_sha: str | None = None,
        branch: str | None = None,
        event: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> dict[str, Any]:
        """Fetch workflow runs matching filters."""
        validate_repo(repo)
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if head_sha:
            params["head_sha"] = head_sha
        if branch:
            params["branch"] = branch
        if event:
            params["event"] = event
        resp = self.request("GET", f"repos/{repo}/actions/runs", params=params)
        return resp.json()

    def get_run_jobs(
        self,
        repo: str,
        run_id: int,
        *,
        attempt: int | None = None,
        per_page: int = 100,
        page: int = 1,
    ) -> dict[str, Any]:
        """Fetch jobs for a workflow run attempt."""
        validate_repo(repo)
        path = (
            f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            if attempt is not None
            else f"repos/{repo}/actions/runs/{run_id}/jobs"
        )
        resp = self.request("GET", path, params={"per_page": per_page, "page": page})
        return resp.json()

    def get_job_logs(self, repo: str, job_id: int) -> str | None:
        """Fetch plain text logs for a specific Actions job."""
        validate_repo(repo)
        try:
            resp = self.request("GET", f"repos/{repo}/actions/jobs/{job_id}/logs")
            return resp.text
        except APIError as err:
            if err.status_code in {404, 410}:
                return None
            raise

    def get_run_logs_archive(self, repo: str, run_id: int, attempt: int | None = None) -> bytes | None:
        """Fetch ZIP log archive for a workflow run."""
        validate_repo(repo)
        path = (
            f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/logs"
            if attempt is not None
            else f"repos/{repo}/actions/runs/{run_id}/logs"
        )
        try:
            resp = self.request("GET", path, raw=True)
            return resp.content
        except APIError as err:
            if err.status_code in {404, 410}:
                return None
            raise

    def get_check_run_annotations(
        self,
        repo: str,
        check_run_id: int,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch check annotations for a check run."""
        validate_repo(repo)
        resp = self.request(
            "GET",
            f"repos/{repo}/check-runs/{check_run_id}/annotations",
            params={"per_page": per_page, "page": page},
        )
        return resp.json()

    def get_code_scanning_alerts(
        self,
        repo: str,
        *,
        ref: str | None = None,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch code scanning alerts for a git reference."""
        validate_repo(repo)
        params: dict[str, Any] = {"per_page": per_page}
        if ref:
            params["ref"] = ref
        # Permission/availability errors must not be disguised as zero findings.
        resp = self.request("GET", f"repos/{repo}/code-scanning/alerts", params=params)
        return resp.json()

    def get_commit_status(self, repo: str, ref: str) -> dict[str, Any]:
        """Fetch combined commit status for a commit ref."""
        validate_repo(repo)
        resp = self.request("GET", f"repos/{repo}/commits/{ref}/status")
        return resp.json()

    def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """Fetch decoded raw file content from repository if available."""
        validate_repo(repo)
        params: dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        try:
            resp = self.request(
                "GET",
                f"repos/{repo}/contents/{path.lstrip('/')}",
                params=params,
                accept="application/vnd.github.raw",
            )
            return resp.text
        except APIError as err:
            if err.status_code == 404:
                return None
            raise

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GitHub GraphQL query."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = self.request("POST", "graphql", json_data=payload)
        data = resp.json()
        if data.get("errors"):
            errors = data.get("errors", [])
            msg = errors[0].get("message", "GraphQL query failed") if errors else "GraphQL error"
            raise APIError(
                f"GraphQL query error: {safe_text(msg, 512)}",
                diagnostic={"classification": "graphql_error", "errors": safe_payload(errors)},
            )
        return data.get("data", {})
