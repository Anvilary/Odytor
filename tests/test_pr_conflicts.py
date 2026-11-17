from test_git_workspace import client_for, run, repository, local_fetch
from git_plus.pr_conflicts import get_pr_conflicts
from git_plus.errors import GitOperationError, RevisionMismatchError
import pytest


def diverge(repo, *, conflict):
    run(repo, 'checkout', '-qb', 'main', 'HEAD')
    (repo / ('src/foo.py' if conflict else 'base.py')).write_text('base change\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'base change')
    base = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    (repo / 'src/foo.py').write_text('head change\n')
    run(repo, 'commit', '-qam', 'head change')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    return base, head


def test_exact_conflicting_region(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=True)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = get_pr_conflicts(client, 'owner/repo', 1, head, base)
    assert result['conflicting'] and result['paths_complete'] and result['regions_complete']
    conflict = result['conflicts'][0]
    assert conflict['path'] == 'src/foo.py'
    assert set(conflict['stages']) == {'1', '2', '3'}
    assert 'base change' in conflict['regions'][0]['text']
    assert 'head change' in conflict['regions'][0]['text']
    assert run(repo, 'rev-parse', 'HEAD').decode().strip() == head
    client.graphql.assert_not_called()


def test_clean_merge(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = get_pr_conflicts(client, 'owner/repo', 1, head)
    assert result['conflicting'] is False
    assert result['conflicts'] == []
    assert result['base_snapshot_is_current'] is True
    assert result['current_base_conflicts_checked'] is True


def test_conflict_rejects_changed_base(local_fetch):
    _, head = local_fetch
    client = client_for(head)
    with pytest.raises(RevisionMismatchError) as exc:
        get_pr_conflicts(client, 'owner/repo', 1, head, 'a'*40)
    assert exc.value.diagnostic['revision'] == 'base'
    assert exc.value.diagnostic['expected_base'] == 'a'*40


def test_base_branch_advanced_but_snapshot_is_ancestor(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    run(repo, 'checkout', '-q', 'main')
    (repo / 'later.py').write_text('advance target after PR snapshot\n')
    run(repo, 'add', 'later.py')
    run(repo, 'commit', '-qm', 'advance base')
    new_base_tip = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = get_pr_conflicts(client, 'owner/repo', 1, head, base)
    assert result['base_sha'] == base
    assert result['base_branch_tip_sha'] == new_base_tip
    assert result['base_snapshot_is_current'] is False
    assert result['current_base_conflicts_checked'] is False
    assert result['merge_scope'] == 'captured_pr_base_sha'
    assert result['conflicting'] is False


def test_unreachable_base_snapshot_fails_with_base_diagnostic(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    run(repo, 'checkout', '-q', 'main')
    # Simulate a rewritten target branch that no longer contains the snapshot.
    run(repo, 'checkout', '-q', '--orphan', 'new-root')
    run(repo, 'rm', '-rfq', '.')
    (repo / 'new-root.txt').write_text('rewritten\n')
    run(repo, 'add', 'new-root.txt')
    run(repo, 'commit', '-qm', 'rewrite target')
    run(repo, 'branch', '-f', 'main', 'HEAD')
    run(repo, 'checkout', '-q', 'feature')
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    with pytest.raises(RevisionMismatchError) as exc:
        get_pr_conflicts(client, 'owner/repo', 1, head, base)
    assert exc.value.diagnostic['revision'] == 'base'
    assert exc.value.diagnostic['expected_base'] == base


def test_structural_conflict_has_explicit_region_limit(local_fetch):
    repo, initial = local_fetch
    run(repo, 'checkout', '-qb', 'main', initial)
    run(repo, 'rm', '-q', 'src/foo.py')
    run(repo, 'commit', '-qm', 'remove file')
    base = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    (repo / 'src/foo.py').write_text('changed\n')
    run(repo, 'commit', '-qam', 'change file')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    result = get_pr_conflicts(client, 'owner/repo', 1, head)
    assert result['paths_complete'] and result['conflicting']
    assert not result['regions_complete']
    assert result['conflicts'][0]['regions_unavailable_reason'] == 'structural_or_binary_conflict'
