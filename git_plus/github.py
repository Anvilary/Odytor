"""GitHub REST and GraphQL API client for Git Plus."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit, urljoin
from typing import Any

import requests

from git_plus.diagnostics import safe_payload, safe_text
from git_plus.errors import APIError, SecurityError, ValidationError

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


class GitHubClient:
    """Stateless GitHub API client with automatic retry, pagination, and redaction."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SecurityError("GitHub API URL must be HTTPS without credentials, query or fragment.")
        if timeout <= 0 or max_retries < 1 or max_response_bytes <= 0:
            raise ValidationError("Timeout and attempt count must be positive.")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.auth = None

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Git-Plus-MCP/0.1.0",
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
                if method.upper() not in {"GET", "HEAD"}:
                    raise APIError("Mutation redirect refused; resolve the canonical repository first.")
                # Never forward API credentials or parameters to signed log URLs.
                if not same_origin:
                    kwargs = {"headers": {"User-Agent": "Git-Plus-MCP/0.1.0"}}
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
        try:
            resp = self.request("GET", f"repos/{repo}/code-scanning/alerts", params=params)
            return resp.json()
        except APIError as err:
            if err.status_code in {403, 404}:
                return []
            raise

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
