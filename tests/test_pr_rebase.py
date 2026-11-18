import pytest

from git_plus.errors import GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from git_plus.git_workspace import GitWorkspace
from git_plus.pr_rebase import rebase_pr
from test_git_workspace import client_for, run, repository, local_fetch
from test_pr_conflicts import diverge


def test_real_linear_rebase_preview(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = rebase_pr(client, 'owner/repo', 1, head, base)
    assert result['status'] == 'rebased'
    assert result['new_sha'] != head
    assert len(result['rewritten_commits']) == 1
    assert not result['applied']
    assert not result['validation']['tests_run']
    assert run(repo, 'rev-parse', 'HEAD').decode().strip() == head


def test_real_conflicting_rebase(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=True)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = rebase_pr(client, 'owner/repo', 1, head, base)
    assert result['status'] == 'conflicts'
    assert result['conflicting_paths'] == ['src/foo.py']
    assert not result['applied']


def test_rebase_requires_explicit_rewrite(local_fetch):
    _, head = local_fetch
    with pytest.raises(SecurityError):
        rebase_pr(client_for(head), 'owner/repo', 1, head, head, apply=True)


def test_up_to_date(local_fetch):
    repo, head = local_fetch
    run(repo, 'branch', 'main', head)
    client = client_for(head)
    result = rebase_pr(client, 'owner/repo', 1, head, head)
    assert result['status'] == 'up_to_date'
    assert result['new_sha'] == head


def test_rebase_rejects_merge_history(local_fetch):
    repo, initial = local_fetch
    base, head = diverge(repo, conflict=False)
    run(repo, 'checkout', '-qb', 'side', initial)
    (repo / 'side.py').write_text('side\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'side')
    run(repo, 'checkout', '-q', 'feature')
    run(repo, 'merge', '-qm', 'merge side', 'side')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    with pytest.raises(ValidationError, match='Merge commits'):
        rebase_pr(client, 'owner/repo', 1, head, base)


def test_rebase_applies_with_branch_lease_and_verifies(local_fetch, monkeypatch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head, fork=True)
    client.get_pr.return_value['base']['sha'] = base
    git = GitWorkspace.git
    pushes = []
    def local_push(self, *args, **kwargs):
        if args[0] in {'push', 'ls-remote'}:
            if args[0] == 'push':
                assert f'--force-with-lease=refs/heads/feature:{head}' in args
                assert 'https://github.com/contributor/repo.git' in args
                pushes.append(args)
                run(repo, 'config', 'receive.denyCurrentBranch', 'ignore')
            args = tuple(str(repo) if a.startswith('https://github.com/') else a for a in args)
            return run(self.path, '-c', 'protocol.file.allow=always', *args)
        return git(self, *args, **kwargs)
    monkeypatch.setattr(GitWorkspace, 'git', local_push)
    result = rebase_pr(client, 'owner/repo', 1, head, base, apply=True, allow_history_rewrite=True)
    assert result['applied'] and result['remote_verified']
    assert run(repo, 'rev-parse', 'feature').decode().strip() == result['new_sha']
    assert run(repo, 'rev-parse', 'feature^').decode().strip() == base
    assert len(pushes) == 1


def test_rebase_rejects_concurrent_update_at_push(local_fetch, monkeypatch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    git = GitWorkspace.git
    def concurrent(self, *args, **kwargs):
        if args[0] == 'ls-remote':  # the live base-tip recheck reads the fixture repository
            args = tuple(str(repo) if a.startswith('https://github.com/') else a for a in args)
            return run(self.path, '-c', 'protocol.file.allow=always', *args)
        if args[0] == 'push':
            # Move the fixture remote after all API checks, before the receive.
            run(repo, 'update-ref', 'refs/heads/feature', base)
            args = tuple(str(repo) if a == 'https://github.com/owner/repo.git' else a for a in args)
            import subprocess
            result = subprocess.run(['git', '-c', 'protocol.file.allow=always', *args], cwd=self.path, capture_output=True)
            assert result.returncode != 0
            assert b'stale info' in result.stdout
            raise GitOperationError('lease rejected')
        return git(self, *args, **kwargs)
    monkeypatch.setattr(GitWorkspace, 'git', concurrent)
    with pytest.raises(GitOperationError) as err:
        rebase_pr(client, 'owner/repo', 1, head, base, apply=True, allow_history_rewrite=True)
    assert err.value.diagnostic['outcome'] == 'unknown'
    assert run(repo, 'rev-parse', 'feature').decode().strip() == base


def test_rebase_preserves_empty_commits(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    run(repo, 'commit', '--allow-empty', '-qm', 'keep empty')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = rebase_pr(client, 'owner/repo', 1, head, base)
    assert len(result['rewritten_commits']) == 2


def test_rebase_base_drift_before_push(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    initial = client.get_pr.return_value
    initial['base']['sha'] = base
    moved = dict(initial, base=dict(initial['base'], sha='a'*40))
    client.get_pr.side_effect = [initial, moved]
    with pytest.raises(RevisionMismatchError):
        rebase_pr(client, 'owner/repo', 1, head, base)


def test_rebase_ignores_hostile_hooks_and_filters(local_fetch, monkeypatch, tmp_path):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    marker = tmp_path / 'executed'
    hook_dir = tmp_path / 'hooks'
    hook_dir.mkdir()
    for name in ('post-checkout', 'pre-rebase', 'post-rewrite'):
        hook = hook_dir / name
        hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
        hook.chmod(0o755)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '2')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(hook_dir))
    monkeypatch.setenv('GIT_CONFIG_KEY_1', 'filter.evil.smudge')
    monkeypatch.setenv('GIT_CONFIG_VALUE_1', f'touch {marker}')
    # Repository attributes alone cannot define executable filter commands.
    (repo / '.gitattributes').write_text('*.py filter=evil\n')
    run(repo, '-c', 'core.hooksPath=/dev/null', 'add', '.gitattributes')
    run(repo, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'attribute')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = rebase_pr(client, 'owner/repo', 1, head, base)
    assert result['status'] == 'rebased'
    assert not marker.exists()


def test_preview_rebases_onto_the_live_tip_when_the_pr_base_snapshot_lags(local_fetch):
    """GitHub's recorded PR base can be an old ancestor of the branch tip; that is exactly when a rebase is wanted."""
    repo, initial = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = initial  # what the PR API still reports: an ancestor of the live tip
    result = rebase_pr(client, 'owner/repo', 1, head, base)
    assert result['status'] == 'rebased' and result['applied'] is False
    assert result['base_sha'] == base and result['base_sha_role'] == 'live_target_branch_tip'
    assert result['pr_api_base_snapshot'] == initial
    assert run(repo, 'rev-parse', 'HEAD').decode().strip() == head


def test_expected_base_must_still_be_the_live_branch_tip(local_fetch):
    repo, initial = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = initial
    with pytest.raises(RevisionMismatchError) as err:  # the stale snapshot is not the tip, and is refused
        rebase_pr(client, 'owner/repo', 1, head, initial)
    assert err.value.diagnostic['actual_base'] == base  # the error names the tip to use


def _local_transport(repo, monkeypatch, *, advance_base_before_push=None):
    git = GitWorkspace.git

    def transport(self, *args, **kwargs):
        if args[0] in {'push', 'ls-remote'}:
            if args[0] == 'ls-remote' and advance_base_before_push and 'refs/heads/main' in args:
                advance_base_before_push()
            if args[0] == 'push':
                run(repo, 'config', 'receive.denyCurrentBranch', 'ignore')
            args = tuple(str(repo) if a.startswith('https://github.com/') else a for a in args)
            return run(self.path, '-c', 'protocol.file.allow=always', *args)
        return git(self, *args, **kwargs)
    monkeypatch.setattr(GitWorkspace, 'git', transport)


def test_apply_with_a_lagging_snapshot_rechecks_the_live_tip_and_pushes(local_fetch, monkeypatch):
    repo, initial = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = initial
    _local_transport(repo, monkeypatch)
    result = rebase_pr(client, 'owner/repo', 1, head, base, apply=True, allow_history_rewrite=True)
    assert result['applied'] and result['remote_verified']
    assert run(repo, 'rev-parse', 'feature^').decode().strip() == base


def test_apply_refuses_when_the_base_branch_advanced_after_the_preview_was_built(local_fetch, monkeypatch):
    repo, initial = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = initial

    def advance():
        run(repo, 'update-ref', 'refs/heads/main', run(repo, 'commit-tree', f'{base}^{{tree}}', '-p', base, '-m', 'moved').decode().strip())
    _local_transport(repo, monkeypatch, advance_base_before_push=advance)
    with pytest.raises(RevisionMismatchError) as err:
        rebase_pr(client, 'owner/repo', 1, head, base, apply=True, allow_history_rewrite=True)
    assert err.value.diagnostic['revision'] == 'base' and err.value.diagnostic['expected_base'] == base
    assert 'moved since the rebase was built' in str(err.value)  # refused by the live-tip recheck, not by the snapshot
    assert run(repo, 'rev-parse', 'feature').decode().strip() == head  # nothing was pushed
