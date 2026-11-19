"""Short-lived Git workspaces with isolated configuration and credentials."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any
from urllib.parse import urlsplit

from odytor.diagnostics import safe_text
from odytor.errors import GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from odytor.github import FORK_WRITE_OPERATIONS, ForkWriteGrant, GitHubClient, validate_repo, validate_sha


def validate_ref(ref: str) -> str:
    if (not ref or ref.startswith(('-', '/', '.')) or ref.endswith(('/', '.', '.lock'))
            or any(c in ref for c in ('..', '@{', '//', '\\'))
            or any(ord(c) <= 32 or ord(c) == 127 or c in '~^:?*[' for c in ref)
            or any(p.startswith('.') or p.endswith('.lock') for p in ref.split('/'))):
        raise ValidationError("Invalid branch reference.")
    return ref


def _verify_fork_network(base_repo: str, target: dict[str, Any]) -> None:
    """Require the head repository to be a fork within the base repository's network."""
    related = {(target.get(k) or {}).get('full_name', '').casefold() for k in ('parent', 'source')}
    if target.get('fork') is not True or base_repo.casefold() not in related:
        raise SecurityError("PR head repository is not a fork in the base repository network.")


# Fetching a large repository or a PR with hundreds of files and long history can take minutes.
GIT_TIMEOUT_SECONDS = 300


def _repo_id(repo: Any) -> int | None:
    """GitHub's stable numeric repository id from trusted API data, when present."""
    value = repo.get('id') if isinstance(repo, dict) else None
    return value if type(value) is int and value > 0 else None


def pr_context(client: GitHubClient, repo: str, number: int, expected_head: str, *,
               write: bool = False, operation: str | None = None) -> dict[str, Any]:
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
        if pr.get('state') != 'open' or pr.get('merged'):
            raise ValidationError("PR must be open and unmerged.")
        if not is_fork and head_ref == base_ref:
            raise SecurityError("Refusing to modify the PR base branch.")
        if not client.token:
            raise SecurityError("GitHub authentication is required for writes.")
        if is_fork:
            _authorize_fork_write(client, repo, number, pr, head_repo, head_ref, actual, operation)
    return {'head_repo': head_repo, 'head_ref': head_ref, 'head_sha': actual,
            'base_repo': base_repo, 'base_repo_id': _repo_id(base.get('repo')), 'base_ref': base_ref, 'base_sha': validate_sha(base.get('sha', '')),
            'is_fork': is_fork, 'state': pr.get('state'), 'merged': bool(pr.get('merged'))}


def _authorize_fork_write(client: GitHubClient, repo: str, number: int, pr: dict[str, Any],
                          head_repo: str, head_ref: str, head_sha: str, operation: str | None) -> None:
    """Require GitHub-confirmed write access to the PR's fork branch; scope a PAT to it."""
    scoped = bool(getattr(client, 'restricted_repo', None))
    if scoped:
        if operation not in FORK_WRITE_OPERATIONS:
            raise SecurityError("Server-side PAT contributor fork writes are limited to additive quick fixes.")
        if (pr.get('head') or {}).get('repo', {}).get('fork') is not True:
            raise SecurityError("PR head repository is not marked as a fork by GitHub.")
        client.authorize_fork_write(ForkWriteGrant(
            principal=getattr(client, 'principal', None) or '', base_repo=repo, number=number,
            head_repo=head_repo, head_ref=head_ref, head_sha=head_sha, operation=operation))
    try:
        target = client.request('GET', f'repos/{head_repo}').json()
        _verify_fork_network(repo, target)
        if (target.get('permissions') or {}).get('push') is not True:
            base_permissions = client.request('GET', f'repos/{repo}').json().get('permissions') or {}
            if not (pr.get('maintainer_can_modify') is True and base_permissions.get('push') is True):
                raise SecurityError(
                    "No direct push permission or permitted maintainer fork access: the PR author "
                    "must allow edits from maintainers and the authenticated user needs push access "
                    "to the base repository.")
    except BaseException:
        if scoped:
            client.revoke_fork_write()
        raise


def verify_context(client: GitHubClient, repo: str, number: int, context: dict[str, Any], *,
                   write: bool = False, base: bool = False, operation: str | None = None) -> None:
    fresh = pr_context(client, repo, number, context['head_sha'], write=write, operation=operation)
    keys = ['head_repo', 'head_ref', 'base_repo', 'base_ref']
    if base:
        keys.append('base_sha')
    if base and fresh['base_sha'] != context['base_sha']:
        raise RevisionMismatchError(context['base_sha'], fresh['base_sha'], revision='base')
    if any(fresh[k] != context[k] for k in keys):
        raise RevisionMismatchError(context['head_sha'], fresh['head_sha'], "PR source or target changed during operation.")


class GitWorkspace:
    """Never inherit Git config, hooks, helpers, filters or trace settings."""

    def __init__(self, client: GitHubClient | None, identity: tuple[str, str] | None = None):
        self.client = client
        self.identity = identity
        self.root: Path | None = None
        self.path: Path | None = None

    def __enter__(self) -> GitWorkspace:
        self.root = Path(tempfile.mkdtemp(prefix='odytor-'))
        self.path = self.root / 'repo'
        self.path.mkdir()
        try:
            self.git('init', '--quiet', '--template=')
        except BaseException:
            shutil.rmtree(self.root, ignore_errors=True)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        # Cleanup is best effort: it must never replace the outcome of an operation that already pushed or
        # failed, and the workspace holds no credentials (they are passed per command through the environment).
        shutil.rmtree(self.root, ignore_errors=True)

    def environment(self, *, network: bool = False, credential_repo: str | None = None,
                    write: bool = False) -> dict[str, str]:
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LANG') if k in os.environ}
        env.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_LITERAL_PATHSPECS='1',
                   GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', GIT_ATTR_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='Odytor', GIT_AUTHOR_EMAIL='noreply@odytor.invalid',
                   GIT_COMMITTER_NAME='Odytor', GIT_COMMITTER_EMAIL='noreply@odytor.invalid')
        if self.identity:
            name, email = self.identity
            env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                       GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        if network and getattr(self.client, 'restricted_repo', None):
            if not credential_repo:
                raise SecurityError("Scoped Git network operations require an explicit repository.")
            validate_repo(credential_repo)
        use_token = bool(network and getattr(self.client, 'token', None))
        if use_token and getattr(self.client, 'restricted_repo', None):
            # Contributor forks are anonymous unless this is the one granted push.
            grant = getattr(self.client, 'fork_grant', None)
            use_token = (credential_repo.casefold() == self.client.restricted_repo
                         or bool(write and grant and credential_repo.casefold() == grant.head_repo.casefold()))
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

    @staticmethod
    def command(*args: str) -> list[str]:
        return ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null',
                '-c', 'credential.helper=', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                '-c', 'http.followRedirects=false', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
                '-c', 'core.autocrlf=false', '-c', 'core.fsmonitor=false',
                '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', *args]

    def git(self, *args: str, input: bytes | None = None, network: bool = False,
            credential_repo: str | None = None, allowed: tuple[int, ...] = (0,),
            write: bool = False) -> bytes:
        if write and (not network or args[:1] != ('push',)):
            raise SecurityError("Credentialed contributor writes are limited to Git push.")
        if network and getattr(self.client, 'restricted_repo', None):
            if (not credential_repo or
                    [a for a in args if a.startswith(self.origin() + '/')] != [self.url(credential_repo)]):
                raise SecurityError("Scoped Git operation must identify exactly its authorized repository URL.")
        command = self.command(*args)
        try:
            result = subprocess.run(command, cwd=self.path,
                                    env=self.environment(network=network, credential_repo=credential_repo, write=write),
                                    input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitOperationError("Git operation failed or timed out.") from exc
        if result.returncode not in allowed:
            text = result.stderr.decode('utf-8', errors='replace')
            token = getattr(self.client, 'token', None)
            if token:
                text = text.replace(token, '[REDACTED]')
            raise GitOperationError(f"Git {args[0]} failed: {safe_text(text, 1024)}")
        return result.stdout

    def fetch(self, repo: str, ref: str, expected: str, label: str, *, depth: int | None = None,
              allow_base_ancestor: bool = False) -> str:
        validate_ref(ref)
        expected = validate_sha(expected)
        if label not in {'head', 'base'}:
            raise ValidationError("Invalid internal fetch label.")
        actual = self.fetch_tip(repo, ref, label, depth=depth)
        return self.verify_fetched_revision(label, expected, actual,
                                            allow_base_ancestor=allow_base_ancestor)

    def fetch_tip(self, repo: str, ref: str, label: str, *, depth: int | None = None) -> str:
        """Fetch a branch and return the revision actually received, without asserting what it should be."""
        validate_ref(ref)
        if label not in {'head', 'base'}:
            raise ValidationError("Invalid internal fetch label.")
        options = ['--depth', str(depth)] if depth else []
        self.git('fetch', '--quiet', '--no-tags', *options, '--', self.url(repo),
                 f'refs/heads/{ref}:refs/odytor/{label}', network=True, credential_repo=repo)
        return self.git('rev-parse', f'refs/odytor/{label}').decode().strip()

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

    def push_fast_forward(self, repo: str, ref: str, expected: str, new_sha: str, operation: str) -> str:
        """Append a descendant commit to one verified branch; never rewrite remote history.

        The explicit lease rejects the push if the remote branch is not exactly the
        expected revision, and the descendant check proves the update is a fast-forward.
        Returns 'applied'; raises GitOperationError with outcome 'rejected' when the
        remote definitively refused it, otherwise outcome 'unknown'.
        """
        validate_ref(ref)
        expected, new_sha = validate_sha(expected), validate_sha(new_sha)
        if getattr(self.client, 'restricted_repo', None) and not self.client.covers_fork_write(repo, ref, expected, operation):
            raise SecurityError("Push target is outside the verified contributor branch grant.")
        try:
            lineage = self.git('rev-list', '--parents', '-n', '1', new_sha).decode().split()
            single_child = lineage == [new_sha, expected]
        except GitOperationError:
            single_child = False
        if not single_child:
            raise SecurityError("Refusing a push that is not exactly one child of the expected head.")
        remote_ref = f'refs/heads/{ref}'
        diagnostic = {'expected_head': expected, 'new_sha': new_sha, 'head_repo': repo, 'head_ref': ref}
        try:
            out = self.git('push', '--porcelain', f'--force-with-lease={remote_ref}:{expected}', '--',
                           self.url(repo), f'{new_sha}:{remote_ref}', network=True, credential_repo=repo,
                           write=True, allowed=(0, 1))
        except GitOperationError as exc:
            raise GitOperationError("Push outcome is uncertain; reconcile the PR head before retrying.",
                                    {**diagnostic, 'outcome': 'unknown'}) from exc
        lines = out.decode('utf-8', errors='replace').splitlines()
        rejected = [line for line in lines if line.startswith('!')]
        if rejected or not any(line.startswith(' \t') for line in lines):
            raise GitOperationError("GitHub rejected the push; no commit was added.",
                                    {**diagnostic, 'outcome': 'rejected' if rejected else 'unknown',
                                     'detail': safe_text(' '.join(rejected), 512)})
        return 'applied'

    def export_tree(self, tree: str, destination: Path, *, max_bytes: int, max_files: int = 200000) -> dict[str, Any]:
        """Write exactly the objects of a tree to a tar file, without a checkout.

        Unlike `git archive` this ignores .gitattributes (export-ignore would silently change
        the tested tree). Submodule entries are skipped and reported; nothing is executed.
        """
        tree = validate_sha(tree)
        listing = self.git('ls-tree', '-r', '-l', '-z', '--full-tree', tree).split(b'\0')
        entries = []
        for raw in filter(None, listing):
            meta, _, path_bytes = raw.partition(b'\t')
            mode, kind, oid, size = meta.decode().split()
            entries.append((mode, kind, oid, 0 if size == '-' else int(size), path_bytes.decode('utf-8', errors='strict')))
        regular = [e for e in entries if e[1] == 'blob']
        skipped = [e[4] for e in entries if e[1] == 'commit']
        if len(regular) > max_files or sum(e[3] for e in regular) > max_bytes:
            raise ValidationError("Repository tree exceeds the configured test artifact limits.")
        proc = subprocess.Popen(self.command('cat-file', '--batch'), cwd=self.path, env=self.environment(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        total = 0
        try:
            with tarfile.open(destination, 'w', format=tarfile.PAX_FORMAT) as archive:
                for mode, _kind, oid, _size, path in regular:
                    if (not path or path.startswith('/') or '\0' in path
                            or any(part in ('', '.', '..') for part in path.split('/'))):
                        raise SecurityError("Repository tree contains an unsafe path.")
                    proc.stdin.write(oid.encode() + b'\n')
                    proc.stdin.flush()
                    header = proc.stdout.readline().split()
                    if len(header) != 3 or header[1] != b'blob':
                        raise GitOperationError("Unexpected git object while exporting the tree.")
                    data = proc.stdout.read(int(header[2]))
                    proc.stdout.read(1)
                    info = tarfile.TarInfo(path)
                    info.mtime = 0
                    if mode == '120000':
                        info.type, info.linkname, info.mode = tarfile.SYMTYPE, data.decode('utf-8', errors='strict'), 0o777
                        archive.addfile(info)
                    else:
                        info.size, info.mode = len(data), 0o755 if mode == '100755' else 0o644
                        archive.addfile(info, io.BytesIO(data))
                    total += len(data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValidationError("Repository tree contains unsupported path or link encoding.") from exc
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.wait()
        return {'files': len(regular), 'bytes': total, 'skipped_submodules': skipped[:20],
                'skipped_submodule_count': len(skipped)}
