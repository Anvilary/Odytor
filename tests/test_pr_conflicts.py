from test_git_workspace import client_for, run, repository, local_fetch
from odytor.pr_conflicts import get_pr_conflicts
from odytor.errors import GitOperationError, RevisionMismatchError
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


def _advance_target_with_conflict(repo, head):
    """Target branch moves past the PR API's snapshot and introduces a conflict only the live tip has."""
    run(repo, 'checkout', '-q', 'main')
    (repo / 'src/foo.py').write_text('later target change\n')
    run(repo, 'commit', '-qam', 'advance base into conflict')
    tip = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    return tip


def test_live_tip_sees_a_conflict_the_stale_snapshot_cannot(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    tip = _advance_target_with_conflict(repo, head)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    snapshot = get_pr_conflicts(client, 'owner/repo', 1, head)
    assert snapshot['conflicting'] is False and snapshot['merge_scope'] == 'captured_pr_base_sha'
    assert snapshot['merged_base_sha'] == base and snapshot['current_base_conflicts_checked'] is False
    live = get_pr_conflicts(client, 'owner/repo', 1, head, merge_target='live_tip')
    assert live['conflicting'] is True and live['conflicts'][0]['path'] == 'src/foo.py'
    assert live['merge_scope'] == 'live_target_branch_tip' and live['merge_target'] == 'live_tip'
    assert live['merged_base_sha'] == tip == live['base_branch_tip_sha']
    assert live['base_sha'] == base and live['base_snapshot_is_current'] is False  # the PR API snapshot is still reported, honestly
    assert live['current_base_conflicts_checked'] is True and live['live_tip_unchanged_after_inspection'] is True


def test_live_tip_expected_base_must_equal_the_tip_not_the_snapshot(local_fetch):
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    tip = _advance_target_with_conflict(repo, head)
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    with pytest.raises(RevisionMismatchError) as exc:
        get_pr_conflicts(client, 'owner/repo', 1, head, base, merge_target='live_tip')
    assert exc.value.diagnostic['expected_base'] == base and exc.value.diagnostic['actual_base'] == tip
    assert get_pr_conflicts(client, 'owner/repo', 1, head, tip, merge_target='live_tip')['merged_base_sha'] == tip


def test_live_tip_works_when_the_snapshot_is_not_an_ancestor(local_fetch):
    """A rewritten target branch cannot be inspected as a snapshot but can still be inspected at its live tip."""
    repo, _ = local_fetch
    base, head = diverge(repo, conflict=False)
    run(repo, 'checkout', '-q', 'main')
    run(repo, 'reset', '-q', '--hard', 'HEAD~1')
    (repo / 'rewritten.py').write_text('x\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'rewritten base')
    tip = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    client = client_for(head)
    client.get_pr.return_value['base']['sha'] = base
    with pytest.raises(RevisionMismatchError):
        get_pr_conflicts(client, 'owner/repo', 1, head)
    assert get_pr_conflicts(client, 'owner/repo', 1, head, merge_target='live_tip')['merged_base_sha'] == tip


def test_unknown_merge_target_is_rejected(local_fetch):
    _, head = local_fetch
    from odytor.errors import ValidationError
    with pytest.raises(ValidationError):
        get_pr_conflicts(client_for(head), 'owner/repo', 1, head, merge_target='current')
