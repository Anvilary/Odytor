"""Short-lived Git workspaces with isolated configuration and credentials."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from git_plus.diagnostics import safe_text
from git_plus.errors import GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from git_plus.github import GitHubClient, validate_repo, validate_sha


def validate_ref(ref: str) -> str:
    if (not ref or ref.startswith(('-', '/', '.')) or ref.endswith(('/', '.', '.lock'))
            or any(c in ref for c in ('..', '@{', '//', '\\'))
            or any(ord(c) <= 32 or ord(c) == 127 or c in '~^:?*[' for c in ref)
            or any(p.startswith('.') or p.endswith('.lock') for p in ref.split('/'))):
        raise ValidationError("Invalid branch reference.")
    return ref


def pr_context(client: GitHubClient, repo: str, number: int, expected_head: str, *, write: bool = False) -> dict[str, Any]:
    validate_repo(repo)
    expected_head = validate_sha(expected_head, "expected_head")
    if type(number) is not int or number <= 0:
        raise ValidationError("PR number must be positive.")
    pr = client.get_pr(repo, number)
    head = pr.get('head') or {}
    base = pr.get('base') or {}
    actual = validate_sha(head.get('sha', ''))
    if actual != expected_head:
        raise RevisionMismatchError(expected_head, actual)
    head_repo = (head.get('repo') or {}).get('full_name', '')
    validate_repo(head_repo)
    base_repo = (base.get('repo') or {}).get('full_name', repo)
    validate_repo(base_repo)
    if base_repo.casefold() != repo.casefold():
        raise SecurityError("PR base repository does not match requested repository.")
    head_ref = validate_ref(head.get('ref', ''))
    base_ref = validate_ref(base.get('ref', ''))
    is_fork = head_repo.casefold() != repo.casefold()
    if write:
        if getattr(client, 'restricted_repo', None) and is_fork:
            raise SecurityError("Server-side PAT cannot mutate contributor forks; use separately authorized credentials.")
        if pr.get('state') != 'open' or pr.get('merged'):
            raise ValidationError("PR must be open and unmerged.")
        if not is_fork and head_ref == base_ref:
            raise SecurityError("Refusing to modify the PR base branch.")
        if not client.token:
            raise SecurityError("GitHub authentication is required for writes.")
        target = client.request('GET', f'repos/{head_repo}').json()
        push = (target.get('permissions') or {}).get('push') is True
        if not push:
            base_permissions = client.request('GET', f'repos/{repo}').json().get('permissions') or {}
            if not (is_fork and pr.get('maintainer_can_modify') is True and base_permissions.get('push') is True):
                raise SecurityError("No direct push permission or permitted maintainer fork access.")
    return {'head_repo': head_repo, 'head_ref': head_ref, 'head_sha': actual,
            'base_repo': base_repo, 'base_ref': base_ref, 'base_sha': validate_sha(base.get('sha', '')),
            'is_fork': is_fork}


def verify_context(client: GitHubClient, repo: str, number: int, context: dict[str, Any], *, write: bool = False, base: bool = False) -> None:
    fresh = pr_context(client, repo, number, context['head_sha'], write=write)
    keys = ['head_repo', 'head_ref', 'base_repo', 'base_ref']
    if base:
        keys.append('base_sha')
    if base and fresh['base_sha'] != context['base_sha']:
        raise RevisionMismatchError(context['base_sha'], fresh['base_sha'], revision='base')
    if any(fresh[k] != context[k] for k in keys):
        raise RevisionMismatchError(context['head_sha'], fresh['head_sha'], "PR source or target changed during operation.")


class GitWorkspace:
    """Never inherit Git config, hooks, helpers, filters or trace settings."""

    def __init__(self, client: GitHubClient):
        self.client = client
        self.root: Path | None = None
        self.path: Path | None = None

    def __enter__(self) -> GitWorkspace:
        self.root = Path(tempfile.mkdtemp(prefix='git-plus-'))
        self.path = self.root / 'repo'
        self.path.mkdir()
        try:
            self.git('init', '--quiet', '--template=')
        except BaseException:
            shutil.rmtree(self.root)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        shutil.rmtree(self.root)

    def environment(self, *, network: bool = False, credential_repo: str | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LANG') if k in os.environ}
        env.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', GIT_ATTR_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='Git Plus', GIT_AUTHOR_EMAIL='git-plus@users.noreply.github.com',
                   GIT_COMMITTER_NAME='Git Plus', GIT_COMMITTER_EMAIL='git-plus@users.noreply.github.com')
        if network and getattr(self.client, 'restricted_repo', None):
            if not credential_repo:
                raise SecurityError("Scoped Git network operations require an explicit repository.")
            validate_repo(credential_repo)
        use_token = bool(network and self.client.token)
        if use_token and getattr(self.client, 'restricted_repo', None):
            use_token = credential_repo.casefold() == self.client.restricted_repo
        if use_token:
            host = self.origin()
            encoded = base64.b64encode(('x-access-token:' + self.client.token).encode()).decode()
            env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0=f'http.{host}/.extraHeader',
                       GIT_CONFIG_VALUE_0='Authorization: Basic ' + encoded)
        return env

    def origin(self) -> str:
        parsed = urlsplit(self.client.base_url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
            raise SecurityError("Git requires a configured HTTPS GitHub API origin.")
        if parsed.hostname == 'api.github.com':
            return 'https://github.com'
        return f'https://{parsed.netloc}'

    def url(self, repo: str) -> str:
        validate_repo(repo)
        return f'{self.origin()}/{repo}.git'

    def git(self, *args: str, input: bytes | None = None, network: bool = False,
            credential_repo: str | None = None, allowed: tuple[int, ...] = (0,)) -> bytes:
        if network and getattr(self.client, 'restricted_repo', None):
            if (not credential_repo or
                    [a for a in args if a.startswith(self.origin() + '/')] != [self.url(credential_repo)]):
                raise SecurityError("Scoped Git operation must identify exactly its authorized repository URL.")
        command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
                   '-c', 'credential.helper=', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                   '-c', 'http.followRedirects=false', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
                   '-c', 'core.autocrlf=false', '-c', 'core.fsmonitor=false', *args]
        try:
            result = subprocess.run(command, cwd=self.path,
                                    env=self.environment(network=network, credential_repo=credential_repo),
                                    input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitOperationError("Git operation failed or timed out.") from exc
        if result.returncode not in allowed:
            text = result.stderr.decode('utf-8', errors='replace')
            if self.client.token:
                text = text.replace(self.client.token, '[REDACTED]')
            raise GitOperationError(f"Git {args[0]} failed: {safe_text(text, 1024)}")
        return result.stdout

    def fetch(self, repo: str, ref: str, expected: str, label: str, *, depth: int | None = None,
              allow_base_ancestor: bool = False) -> str:
        validate_ref(ref)
        expected = validate_sha(expected)
        if label not in {'head', 'base'}:
            raise ValidationError("Invalid internal fetch label.")
        options = ['--depth', str(depth)] if depth else []
        self.git('fetch', '--quiet', '--no-tags', *options, '--', self.url(repo),
                 f'refs/heads/{ref}:refs/git-plus/{label}', network=True, credential_repo=repo)
        actual = self.git('rev-parse', f'refs/git-plus/{label}').decode().strip()
        return self.verify_fetched_revision(label, expected, actual,
                                            allow_base_ancestor=allow_base_ancestor)

    def verify_fetched_revision(self, label: str, expected: str, actual: str,
                                *, allow_base_ancestor: bool = False) -> str:
        """Allow snapshot-only reads when the PR base is an ancestor of the remote branch tip."""
        if actual == expected:
            return actual
        if label == 'base' and allow_base_ancestor:
            try:
                common = self.git('merge-base', expected, actual).decode().strip()
            except GitOperationError:
                common = None
            if common == expected:
                return actual
        raise RevisionMismatchError(expected, actual, revision=label)
