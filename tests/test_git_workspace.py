"""Real Git fixtures, with all remote access replaced by local fetches."""
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from odytor.errors import RevisionMismatchError, SecurityError, ValidationError
from odytor.git_workspace import GitWorkspace, pr_context, validate_ref
from odytor.github import GitHubClient


def run(path, *args, input=None):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=path, input=input)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    run(repo, 'init', '-q', '--initial-branch=feature')
    run(repo, 'config', 'user.name', 'Fixture')
    run(repo, 'config', 'user.email', 'fixture@example.com')
    (repo / 'src').mkdir()
    (repo / 'src/foo.py').write_text('line 1\nold line 2\nline 3\n')
    (repo / 'other.py').write_text('old\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'initial')
    return repo, run(repo, 'rev-parse', 'HEAD').decode().strip()


@pytest.fixture
def local_fetch(monkeypatch, repository):
    repo, sha = repository
    def fetch(self, target, ref, expected, label, **kwargs):
        # Only the fixture transport allows file access. Production stays HTTPS-only.
        run(self.path, '-c', 'protocol.file.allow=always', 'fetch', '-q', str(repo), f'{ref}:refs/odytor/{label}')
        actual = self.git('rev-parse', f'refs/odytor/{label}').decode().strip()
        return self.verify_fetched_revision(label, expected, actual,
                                            allow_base_ancestor=kwargs.get('allow_base_ancestor', False))
    monkeypatch.setattr(GitWorkspace, 'fetch', fetch)
    return repo, sha


def client_for(sha, *, fork=False, push=True):
    client = Mock(spec=GitHubClient)
    client.token = 'private-test-token'
    client.base_url = 'https://api.github.com'
    client.get_pr.return_value = {'state': 'open', 'head': {'sha': sha, 'ref': 'feature',
        'repo': {'full_name': 'contributor/repo' if fork else 'owner/repo'}},
        'base': {'sha': sha, 'ref': 'main', 'repo': {'full_name': 'owner/repo'}},
        'maintainer_can_modify': False}
    client.request.return_value.json.return_value = {'permissions': {'push': push}, 'fork': True,
        'parent': {'full_name': 'owner/repo'}, 'source': {'full_name': 'owner/repo'}}
    return client


def test_workspace_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(tmp_path))
    monkeypatch.setenv('GIT_TRACE', '/tmp/should-not-exist')
    client = client_for('a'*40)
    with GitWorkspace(client) as work:
        root = work.root
        env = work.environment()
        assert 'GIT_TRACE' not in env and 'GIT_CONFIG_COUNT' not in env
        assert 'private-test-token' not in str(work.git('config', '--list'))
        assert work.url('owner/repo') == 'https://github.com/owner/repo.git'
    assert not root.exists()


@pytest.mark.parametrize('ref', ['-bad', '../bad', 'a..b', 'a/.b', 'a.lock/x', 'a:b', 'a b'])
def test_invalid_ref(ref):
    with pytest.raises(ValidationError):
        validate_ref(ref)


def test_fork_permissions_and_stale_head():
    client = client_for('a'*40, fork=True, push=False)
    with pytest.raises(SecurityError):
        pr_context(client, 'owner/repo', 1, 'a'*40, write=True)
    with pytest.raises(RevisionMismatchError):
        pr_context(client, 'owner/repo', 1, 'b'*40, write=True)


def test_workspace_cleanup_failure_never_replaces_the_operation_result(monkeypatch):
    """A rebase pushes and verifies inside the workspace; a later cleanup error must not turn that into a failure."""
    import shutil
    real = shutil.rmtree

    def strict_rmtree(path, ignore_errors=False, **kwargs):
        real(path, ignore_errors=True)
        if not ignore_errors:
            raise OSError(16, 'Device or resource busy')  # what an unguarded cleanup would hit
    monkeypatch.setattr('odytor.git_workspace.shutil.rmtree', strict_rmtree)
    with GitWorkspace(Mock(token=None)) as work:
        assert work.path.is_dir()
        result = {'applied': True, 'remote_verified': True}
    assert result['applied'] is True and not work.root.exists()
