"""Contributor-fork quick fixes: authorization, credential scope and Git semantics."""
import json
import subprocess
from unittest.mock import MagicMock, Mock

import pytest
import requests

from odytor.errors import (APIError, GitOperationError, RevisionMismatchError, SecurityError,
                             ValidationError)
from odytor.git_workspace import GitWorkspace, pr_context
from odytor.github import ForkWriteGrant, GitHubClient
from odytor.quick_fix import apply_quick_fix
from test_git_workspace import client_for, repository, local_fetch, run
from test_quick_fix import SIMPLE_PATCH

BASE = 'owner/repo'
FORK = 'contributor/repo'
PAT = 'ghp_fork_pat_must_not_leak'
NEW_CONTENT = b'line 1\nnew line 2\nline 3\n'


def fork_client(sha, *, push=False, maintainer=True, base_push=True, parent=BASE, is_fork=True,
                marked_fork=True, error=None):
    """Mocked GitHub API for an authorization-boundary scenario."""
    client = client_for(sha, fork=True, push=push)
    pr = client.get_pr.return_value
    pr['maintainer_can_modify'] = maintainer
    pr['head']['repo']['fork'] = marked_fork
    client.principal = 'alice'
    def request(method, path, **kwargs):
        if error:
            raise error
        data = {
            f'repos/{FORK}': {'fork': is_fork, 'permissions': {'push': push},
                              'parent': {'full_name': parent}, 'source': {'full_name': parent}},
            f'repos/{BASE}': {'permissions': {'push': base_push}},
        }[path]
        return Mock(json=Mock(return_value=data))
    client.request.side_effect = request
    return client


@pytest.fixture
def fork_remote(monkeypatch, local_fetch):
    """Local Git transport for the fork URL; records push/ls-remote invocations."""
    repo, sha = local_fetch
    run(repo, 'config', 'receive.denyCurrentBranch', 'ignore')
    real = GitWorkspace.git
    calls = []
    behaviour = {'before_push': None, 'fail_ls_remote': False}
    def git(self, *args, **kwargs):
        if args[0] not in {'push', 'ls-remote'}:
            return real(self, *args, **kwargs)
        env = self.environment(network=True, credential_repo=kwargs.get('credential_repo'),
                               write=kwargs.get('write', False))
        calls.append((args, kwargs, env))
        if args[0] == 'push' and behaviour['before_push']:
            behaviour['before_push']()
        if args[0] == 'ls-remote' and behaviour['fail_ls_remote']:
            raise GitOperationError('network down')
        local = tuple(str(repo) if a.startswith('https://github.com/') else a for a in args)
        result = subprocess.run(['git', '-c', 'protocol.file.allow=always', *local], cwd=self.path,
                                capture_output=True, env={**env, 'GIT_CONFIG_NOSYSTEM': '1'})
        if result.returncode not in kwargs.get('allowed', (0,)):
            raise GitOperationError('git failed')
        return result.stdout
    monkeypatch.setattr(GitWorkspace, 'git', git)
    return repo, sha, calls, behaviour


def pushes(calls):
    return [c for c in calls if c[0][0] == 'push']


def test_authorized_fork_quick_fix_fast_forwards_exact_branch(fork_remote):
    repo, sha, calls, _ = fork_remote
    client = fork_client(sha)
    result = apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix line\n\nDetails')
    new = run(repo, 'rev-parse', 'feature').decode().strip()
    assert result['applied'] and result['new_sha'] == new and result['remote_verified']
    assert result['is_fork'] and result['mechanism'] == 'git_push_fast_forward'
    assert result['validation']['tests_run'] is False and result['signed'] is False
    assert run(repo, 'rev-parse', 'feature^').decode().strip() == sha
    assert run(repo, 'show', 'feature:src/foo.py') == NEW_CONTENT
    assert run(repo, 'log', '-1', '--format=%an <%ae>|%cn <%ce>|%B', 'feature').decode().startswith(
        'alice <alice@users.noreply.github.com>|alice <alice@users.noreply.github.com>|Fix line\n\nDetails')
    [(args, kwargs, _)] = pushes(calls)
    assert f'--force-with-lease=refs/heads/feature:{sha}' in args
    assert not any(a in {'--force', '-f', '--mirror', '--delete'} or a.startswith('+') for a in args)
    assert args[-2:] == ('https://github.com/contributor/repo.git', f'{new}:refs/heads/feature')
    assert kwargs['write'] is True
    client.graphql.assert_not_called()


def test_same_repository_quick_fix_still_uses_atomic_graphql(local_fetch):
    _, sha = local_fetch
    client = client_for(sha)
    client.graphql.side_effect = APIError('lost')
    with pytest.raises(APIError) as err:
        apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert err.value.diagnostic['outcome'] == 'unknown'
    client.graphql.assert_called_once()


@pytest.mark.parametrize('options, message', [
    ({'maintainer': False}, 'maintainer'),
    ({'base_push': False}, 'maintainer'),
    ({'parent': 'someone/else'}, 'network'),
    ({'is_fork': False}, 'network'),
])
def test_unauthorized_fork_writes_never_reach_git(fork_remote, options, message):
    repo, sha, calls, _ = fork_remote
    client = fork_client(sha, **options)
    with pytest.raises(SecurityError, match=message):
        apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert not pushes(calls)
    assert run(repo, 'rev-parse', 'feature').decode().strip() == sha


def test_github_permission_error_is_surfaced_without_push(fork_remote):
    repo, sha, calls, _ = fork_remote
    client = fork_client(sha, error=APIError('forbidden', status_code=403))
    with pytest.raises(APIError):
        apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert not pushes(calls)


def test_stale_head_and_head_moved_during_preparation(fork_remote):
    _, sha, calls, _ = fork_remote
    client = fork_client(sha)
    with pytest.raises(RevisionMismatchError):
        apply_quick_fix(client, BASE, 1, 'b' * 40, SIMPLE_PATCH, 'Fix')
    first = client.get_pr.return_value
    moved = dict(first, head=dict(first['head'], sha='c' * 40))
    client.get_pr.side_effect = [first, moved]
    with pytest.raises(RevisionMismatchError):
        apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert not pushes(calls)


def test_remote_branch_moves_before_push_is_rejected_by_lease(fork_remote):
    repo, sha, calls, behaviour = fork_remote
    other = run(repo, 'commit-tree', f'{sha}^{{tree}}', '-p', sha, '-m', 'concurrent').decode().strip()
    behaviour['before_push'] = lambda: run(repo, 'update-ref', 'refs/heads/feature', other)
    with pytest.raises(GitOperationError) as err:
        apply_quick_fix(fork_client(sha), BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert err.value.diagnostic['outcome'] == 'rejected'
    assert run(repo, 'rev-parse', 'feature').decode().strip() == other
    assert len(pushes(calls)) == 1


def test_push_transport_failure_is_uncertain_and_never_retried(fork_remote, monkeypatch):
    repo, sha, calls, _ = fork_remote
    real = GitWorkspace.git
    def failing(self, *args, **kwargs):
        if args[0] == 'push':
            calls.append((args, kwargs, {}))
            raise GitOperationError('connection reset')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(GitWorkspace, 'git', failing)
    with pytest.raises(GitOperationError) as err:
        apply_quick_fix(fork_client(sha), BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert err.value.diagnostic['outcome'] == 'unknown'
    assert len(pushes(calls)) == 1


def test_lost_remote_verification_is_uncertain(fork_remote):
    repo, sha, calls, behaviour = fork_remote
    behaviour['fail_ls_remote'] = True
    with pytest.raises(GitOperationError) as err:
        apply_quick_fix(fork_client(sha), BASE, 1, sha, SIMPLE_PATCH, 'Fix')
    assert err.value.diagnostic['outcome'] == 'unknown'
    assert len(pushes(calls)) == 1


def test_non_pushed_ref_other_than_grant_is_refused(local_fetch):
    _, sha = local_fetch
    client = GitHubClient(token=PAT, restricted_repo=BASE, principal='alice', session=MagicMock())
    client.authorize_fork_write(ForkWriteGrant('alice', BASE, 1, FORK, 'feature', sha, 'quick_fix'))
    with GitWorkspace(client) as work:
        for repo_name, ref, head in ((FORK, 'main', sha), ('other/repo', 'feature', sha), (FORK, 'feature', 'd' * 40)):
            with pytest.raises(SecurityError, match='grant'):
                work.push_fast_forward(repo_name, ref, head, 'e' * 40, 'quick_fix')
        with pytest.raises(SecurityError, match='grant'):
            work.push_fast_forward(FORK, 'feature', sha, 'e' * 40, 'rebase')


def test_push_requires_exactly_one_child_commit(local_fetch):
    repo, sha = local_fetch
    client = Mock(spec=GitHubClient)
    client.token = 'tok'
    client.base_url = 'https://api.github.com'
    with GitWorkspace(client) as work:
        run(work.path, '-c', 'protocol.file.allow=always', 'fetch', '-q', str(repo), 'feature')
        tree = run(work.path, 'rev-parse', 'FETCH_HEAD^{tree}').decode().strip()
        child = run(work.path, 'commit-tree', tree, '-p', sha, '-m', 'a').decode().strip()
        grandchild = run(work.path, 'commit-tree', tree, '-p', child, '-m', 'b').decode().strip()
        sibling = run(work.path, 'commit-tree', tree, '-m', 'unrelated root').decode().strip()
        for bad in (grandchild, sibling, sha):
            with pytest.raises(SecurityError, match='one child'):
                work.push_fast_forward(FORK, 'feature', sha, bad, 'quick_fix')


# ---- Scoped PAT boundary -------------------------------------------------------------

def response(status=200, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r._content_consumed = True
    r.headers.update(headers or {})
    return r


def pat_client(routes, sent):
    def handler(method, url, **kwargs):
        sent.append((method, url, kwargs.get('headers', {}).get('Authorization')))
        key = url.removeprefix('https://api.github.com/')
        return routes[key]() if key in routes else response(404, {'message': 'Not Found'})
    session = MagicMock()
    session.request.side_effect = handler
    return GitHubClient(token=PAT, restricted_repo=BASE, principal='alice', session=session)


def pr_json(sha, **kw):
    return {'state': 'open', 'merged': False, 'maintainer_can_modify': kw.get('maintainer', True),
            'head': {'sha': sha, 'ref': 'feature', 'repo': {'full_name': FORK, 'fork': True}},
            'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'full_name': BASE}}}


def pat_routes(sha, **kw):
    return {
        f'repos/{BASE}/pulls/1': lambda: response(200, pr_json(sha, **kw)),
        f'repos/{FORK}': lambda: response(200, {'fork': True, 'permissions': {'push': False},
                                                'parent': {'full_name': BASE}, 'source': {'full_name': BASE}}),
        f'repos/{BASE}': lambda: response(200, {'permissions': {'push': kw.get('base_push', True)}}),
    }


def test_pat_fork_authorization_sends_credentials_only_to_pr_repositories():
    sent = []
    client = pat_client(pat_routes('a' * 40), sent)
    context = pr_context(client, BASE, 1, 'a' * 40, write=True, operation='quick_fix')
    assert context['is_fork'] and client.fork_grant == ForkWriteGrant(
        'alice', BASE, 1, FORK, 'feature', 'a' * 40, 'quick_fix')
    assert {u for _, u, _ in sent} == {f'https://api.github.com/repos/{BASE}/pulls/1',
                                       f'https://api.github.com/repos/{FORK}',
                                       f'https://api.github.com/repos/{BASE}'}
    assert all(m == 'GET' and auth == f'Bearer {PAT}' for m, _, auth in sent)


@pytest.mark.parametrize('kwargs', [{'maintainer': False}, {'base_push': False}])
def test_pat_without_maintainer_access_gets_no_lasting_grant(kwargs):
    client = pat_client(pat_routes('a' * 40, **kwargs), [])
    with pytest.raises(SecurityError):
        pr_context(client, BASE, 1, 'a' * 40, write=True, operation='quick_fix')
    assert client.fork_grant is None


def test_pat_grant_does_not_expand_to_other_requests():
    sent = []
    client = pat_client(pat_routes('a' * 40), sent)
    pr_context(client, BASE, 1, 'a' * 40, write=True, operation='quick_fix')
    sent.clear()
    for method, path in [
        ('POST', f'repos/{FORK}'), ('DELETE', f'repos/{FORK}'),
        ('GET', f'repos/{FORK}/contents/README.md'), ('GET', f'repos/{FORK}/git/refs'),
        ('GET', f'repos/{FORK}?x=1'), ('GET', 'repos/contributor/other'),
        ('GET', 'repos/contributor/unlisted-repo'), ('GET', 'user'), ('GET', 'orgs/owner/repos'),
    ]:
        with pytest.raises(SecurityError):
            client.request(method, path)
    mutation = ('mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) '
                '{ commit { oid } } }')
    with pytest.raises(SecurityError):
        client.graphql(mutation, {'input': {'branch': {'repositoryNameWithOwner': FORK, 'branchName': 'feature'}}})
    assert not sent


def test_pat_grant_cannot_follow_redirect_to_another_repository():
    sent = []
    routes = pat_routes('a' * 40)
    routes[f'repos/{FORK}'] = lambda: response(301, {}, {'Location': 'https://api.github.com/repos/evil/repo'})
    client = pat_client(routes, sent)
    with pytest.raises(SecurityError):
        pr_context(client, BASE, 1, 'a' * 40, write=True, operation='quick_fix')
    assert client.fork_grant is None
    assert all('evil' not in u for _, u, _ in sent)


def test_grant_requires_matching_principal_scope_and_operation():
    sha = 'a' * 40
    client = GitHubClient(token=PAT, restricted_repo=BASE, principal='alice', session=MagicMock())
    for grant in (
        ForkWriteGrant('mallory', BASE, 1, FORK, 'feature', sha, 'quick_fix'),
        ForkWriteGrant('alice', 'other/repo', 1, FORK, 'feature', sha, 'quick_fix'),
        ForkWriteGrant('alice', BASE, 1, BASE, 'feature', sha, 'quick_fix'),
        ForkWriteGrant('alice', BASE, 1, FORK, 'feature', sha, 'rebase'),
        ForkWriteGrant('alice', BASE, 0, FORK, 'feature', sha, 'quick_fix'),
    ):
        with pytest.raises(SecurityError):
            client.authorize_fork_write(grant)
    anonymous = GitHubClient(token=PAT, restricted_repo=BASE, session=MagicMock())
    with pytest.raises(SecurityError):
        anonymous.authorize_fork_write(ForkWriteGrant('alice', BASE, 1, FORK, 'feature', sha, 'quick_fix'))
    with pytest.raises(SecurityError):
        GitHubClient(token='t', session=MagicMock()).authorize_fork_write(
            ForkWriteGrant('alice', BASE, 1, FORK, 'feature', sha, 'quick_fix'))


def test_pat_only_enters_git_for_the_granted_push():
    sha = 'a' * 40
    client = GitHubClient(token=PAT, restricted_repo=BASE, principal='alice', session=MagicMock())
    with GitWorkspace(client) as work:
        no_grant = work.environment(network=True, credential_repo=FORK, write=True)
        assert PAT not in ''.join(no_grant.values())
        client.authorize_fork_write(ForkWriteGrant('alice', BASE, 1, FORK, 'feature', sha, 'quick_fix'))
        assert PAT not in ''.join(work.environment(network=True, credential_repo=FORK).values())
        assert PAT not in ''.join(work.environment(network=True, credential_repo='unrelated/repo', write=True).values())
        push_env = work.environment(network=True, credential_repo=FORK, write=True)
        assert push_env['GIT_CONFIG_KEY_0'] == 'http.https://github.com/.extraHeader'
        with pytest.raises(SecurityError):  # write credentials are push-only
            work.git('fetch', '--', work.url(FORK), network=True, credential_repo=FORK, write=True)
        with pytest.raises(SecurityError):  # exactly the authorized URL
            work.git('push', '--', work.url('unrelated/repo'), 'x:y', network=True, credential_repo=FORK, write=True)


def test_git_errors_never_contain_the_pat(monkeypatch):
    client = GitHubClient(token=PAT, restricted_repo=BASE, principal='alice', session=MagicMock())
    client.authorize_fork_write(ForkWriteGrant('alice', BASE, 1, FORK, 'feature', 'a' * 40, 'quick_fix'))
    done = subprocess.CompletedProcess([], 128, b'', f'fatal: could not read {PAT} from remote'.encode())
    with GitWorkspace(client) as work:
        monkeypatch.setattr(subprocess, 'run', lambda *a, **k: done)
        with pytest.raises(GitOperationError) as err:
            work.git('push', '--', work.url(FORK), 'x:y', network=True, credential_repo=FORK, write=True)
    assert PAT not in str(err.value) and PAT not in json.dumps(err.value.to_dict())


def test_restricted_quick_fix_end_to_end_with_scoped_pat(fork_remote):
    repo, sha, calls, _ = fork_remote
    sent = []
    routes = pat_routes(sha)
    client = pat_client(routes, sent)
    result = apply_quick_fix(client, BASE, 1, sha, SIMPLE_PATCH, 'Fix line')
    assert result['applied'] and result['remote_verified']
    assert run(repo, 'rev-parse', 'feature').decode().strip() == result['new_sha']
    for args, kwargs, env in calls:
        has_pat = 'GIT_CONFIG_COUNT' in env
        assert has_pat == (args[0] == 'push')
        assert PAT not in ''.join(v for k, v in env.items() if k != 'GIT_CONFIG_VALUE_0')
    assert all(m == 'GET' for m, _, _ in sent)
    assert PAT not in json.dumps(result)


def test_restricted_pat_rebase_into_fork_remains_blocked(local_fetch):
    _, sha = local_fetch
    from odytor.pr_rebase import rebase_pr
    client = pat_client(pat_routes(sha), [])
    with pytest.raises(SecurityError, match='additive single-commit operations'):
        rebase_pr(client, BASE, 1, sha, 'b' * 40, apply=True, allow_history_rewrite=True)
    assert client.fork_grant is None
