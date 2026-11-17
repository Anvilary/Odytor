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


def test_conflict_rejects_changed_base(local_fetch):
    _, head = local_fetch
    client = client_for(head)
    with pytest.raises(RevisionMismatchError):
        get_pr_conflicts(client, 'owner/repo', 1, head, 'a'*40)


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
