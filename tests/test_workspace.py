"""Disposable workspaces: exact revisions, versioned edits, immutable snapshots and strict confinement."""
import os
import subprocess
import tarfile
import threading

import pytest

from odytor.config import WorkspaceSettings
from odytor.errors import RevisionMismatchError, SecurityError, ValidationError
from odytor.workspace import VersionConflictError, WorkspaceService, open_workspace
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

OWNER = 'alice'


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(tmp_path, clock):
    return WorkspaceService(WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024), clock=clock)


@pytest.fixture
def opened(service, local_fetch):
    """A workspace on a repository holding a symlink, an executable and a binary file."""
    repo, sha = local_fetch
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    (repo / 'tools').mkdir()
    (repo / 'tools/run.sh').write_text('#!/bin/sh\necho hi\n')
    (repo / 'tools/run.sh').chmod(0o755)
    (repo / 'data.bin').write_bytes(b'\x00\x01\x02binary')
    (repo / 'src/link').symlink_to('../tests')
    (repo / 'src/mod.py').write_text('import os\n\nclass Alpha:\n    def run(self):\n        return 1\n\nasync def beta():\n    pass\n\ndef gamma():\n    return Alpha\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'more')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    summary = open_workspace(client, 'owner/repo', service=service, owner=OWNER, number=1, expected_head=head)
    return service, summary, repo, head


def ws(opened):
    return opened[1]['workspace_id']


def test_create_binds_exact_revision_and_exposes_no_checkout(opened, service):
    _, summary, repo, head = opened
    assert summary['version'] == 0 and summary['modified'] is False and summary['tree'] == summary['source_tree']
    assert summary['source']['kind'] == 'pr_head' and summary['source']['head_sha'] == head and summary['repo'] == 'owner/repo'
    assert summary['source_tree'] == run(repo, 'rev-parse', 'HEAD^{tree}').decode().strip()
    assert summary['scope']['operations'] and summary['limits']['max_changed_files'] == 300
    # no working directory exists: the only entry in the repository directory is .git
    assert os.listdir(service._dir(ws(opened)) / 'repo') == ['.git']
    assert oct(service._dir(ws(opened)).stat().st_mode & 0o777) == '0o700'


def test_wrong_head_is_rejected_and_nothing_is_left_behind(service, local_fetch):
    repo, sha = local_fetch
    client = client_for(sha)
    with pytest.raises(RevisionMismatchError):
        open_workspace(client, 'owner/repo', service=service, owner=OWNER, number=1, expected_head='b' * 40)
    assert list(service.root.glob('ws-*')) == []


def test_branch_source_verifies_the_exact_sha(service, local_fetch):
    repo, sha = local_fetch
    client = client_for(sha)
    summary = open_workspace(client, 'owner/repo', service=service, owner=OWNER, source='branch', branch='feature', expected_sha=sha)
    assert summary['source'] == {'kind': 'branch', 'branch': 'feature', 'head_sha': sha, 'head_repo': 'owner/repo'}
    with pytest.raises(RevisionMismatchError):
        open_workspace(client, 'owner/repo', service=service, owner=OWNER, source='branch', branch='feature', expected_sha='c' * 40)
    assert len(list(service.root.glob('ws-*'))) == 1


def test_prospective_merge_workspace_holds_the_merged_tree(service, local_fetch):
    repo, base = local_fetch
    run(repo, 'checkout', '-qb', 'main', 'HEAD')
    (repo / 'base_only.py').write_text('b\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'base')
    tip = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    (repo / 'head_only.py').write_text('h\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'head')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(head)
    summary = open_workspace(client, 'owner/repo', service=service, owner=OWNER, source='prospective_merge', number=1,
                             expected_head=head, expected_base=tip)
    assert summary['source']['base_sha'] == tip and summary['source']['base_sha_role'] == 'live_target_branch_tip'
    paths = {e['path'] for e in service.tree(summary['workspace_id'], OWNER, recursive=True)['entries']}
    assert {'base_only.py', 'head_only.py'} <= paths
    with pytest.raises(ValidationError):  # expected_base is mandatory for a merge and forbidden otherwise
        open_workspace(client, 'owner/repo', service=service, owner=OWNER, source='prospective_merge', number=1, expected_head=head)


def test_conflicting_merge_is_refused_with_paths(service, local_fetch):
    repo, base = local_fetch
    run(repo, 'checkout', '-qb', 'main', 'HEAD')
    (repo / 'src/foo.py').write_text('base side\n')
    run(repo, 'commit', '-qam', 'b')
    tip = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    (repo / 'src/foo.py').write_text('head side\n')
    run(repo, 'commit', '-qam', 'h')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    with pytest.raises(ValidationError) as info:
        open_workspace(client_for(head), 'owner/repo', service=service, owner=OWNER, source='prospective_merge', number=1,
                       expected_head=head, expected_base=tip)
    assert info.value.diagnostic['stage'] == 'merge_conflict' and info.value.diagnostic['paths'] == ['src/foo.py']
    assert list(service.root.glob('ws-*')) == []


# --- reading ---------------------------------------------------------------------------------------------------

def test_tree_read_search_symbols(opened):
    service = opened[0]
    w = ws(opened)
    top = service.tree(w, OWNER)
    assert {e['path'] for e in top['entries']} >= {'src', 'tests', 'data.bin'} and top['complete']
    deep = service.tree(w, OWNER, path='src')
    assert {'src/mod.py', 'src/foo.py', 'src/link'} <= {e['path'] for e in deep['entries']}
    assert next(e for e in deep['entries'] if e['path'] == 'src/link')['symlink'] is True
    one = service.read(w, OWNER, path='src/mod.py', start_line=3, end_line=5)
    assert [l['n'] for l in one['lines']] == [3, 4, 5] and one['lines'][0]['text'] == 'class Alpha:' and one['total_lines'] == 11
    assert one['next_start_line'] == 6 and one['version'] == 0 and one['blob_sha']
    assert service.read(w, OWNER, path='data.bin')['binary'] is True
    assert service.read(w, OWNER, path='src/link')['symlink_target'] == '../tests'
    found = service.search(w, OWNER, pattern='Alpha')
    assert [(m['path'], m['line']) for m in found['matches']] == [('src/mod.py', 3), ('src/mod.py', 11)] and found['complete']
    assert service.search(w, OWNER, pattern='nomatchanywhere')['matches'] == []
    assert service.search(w, OWNER, pattern='^def ', fixed=False, path='src')['matches_counted'] == 1
    symbols = service.symbols(w, OWNER, path='src/mod.py')
    assert {(s['name'], s['kind']) for s in symbols['symbols']} == {('Alpha', 'class'), ('run', 'function'), ('beta', 'function'), ('gamma', 'function')}


def test_read_is_bounded_and_missing_or_directory_paths_fail_cleanly(opened):
    service = opened[0]
    w = ws(opened)
    big = '\n'.join(f'line {i}' for i in range(1000)) + '\n'
    service.write_file(w, OWNER, expected_version=0, path='big.txt', content=big)
    page = service.read(w, OWNER, path='big.txt')
    assert len(page['lines']) == 400 and page['next_start_line'] == 401 and page['complete'] is False
    assert service.read(w, OWNER, path='big.txt', start_line=401, max_bytes=300)['lines'][0]['n'] == 401
    with pytest.raises(ValidationError):
        service.read(w, OWNER, path='nope.py')
    with pytest.raises(ValidationError):
        service.read(w, OWNER, path='src')


@pytest.mark.parametrize('bad', ['../x', '/etc/passwd', '.git/config', 'a/../../b', 'a//b', 'a\\b', 'x\x00y', '', 'dir/', 'a/./b', '.GIT/x', 'a/.git/b'])
def test_path_attacks_are_rejected_for_every_operation(opened, bad):
    service = opened[0]
    w = ws(opened)
    calls = [lambda: service.read(w, OWNER, path=bad), lambda: service.write_file(w, OWNER, expected_version=0, path=bad, content='x'),
             lambda: service.delete_path(w, OWNER, expected_version=0, path=bad)]
    if bad:  # an empty search path simply means "the whole workspace"
        calls.append(lambda: service.search(w, OWNER, pattern='x', path=bad))
    for call in calls:
        with pytest.raises((SecurityError, ValidationError)):
            call()
    assert service.get(w, OWNER, operation='read')['version'] == 0


def test_glob_characters_are_literal(opened):
    service = opened[0]
    w = ws(opened)
    with pytest.raises(ValidationError):
        service.read(w, OWNER, path='src/*')
    assert service.search(w, OWNER, pattern='Alpha', path='src/*')['matches'] == []


# --- editing ---------------------------------------------------------------------------------------------------

def test_edits_are_versioned_and_report_exact_identity(opened):
    service = opened[0]
    w = ws(opened)
    first = service.write_file(w, OWNER, expected_version=0, path='tests/test_new.py', content='def test_n():\n    assert 1\n')
    assert first['changed'] and first['version'] == 1 and first['modified'] and first['paths'] == ['tests/test_new.py']
    assert first['tree'] != first['source_tree']
    assert service.read(w, OWNER, path='tests/test_new.py')['version'] == 1
    same = service.write_file(w, OWNER, expected_version=1, path='tests/test_new.py', content='def test_n():\n    assert 1\n')
    assert same['changed'] is False and same['version'] == 1  # identical content is not a new version
    with pytest.raises(VersionConflictError) as info:
        service.write_file(w, OWNER, expected_version=0, path='tests/test_new.py', content='stale')
    assert info.value.diagnostic['current_version'] == 1 and info.value.diagnostic['current_tree'] == first['tree']
    assert service.read(w, OWNER, path='tests/test_new.py')['lines'][1]['text'] == '    assert 1'


def test_write_preserves_executable_mode_and_refuses_special_entries(opened):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='tools/run.sh', content='#!/bin/sh\necho changed\n')
    assert service.read(w, OWNER, path='tools/run.sh')['mode'] == '100755'
    with pytest.raises(ValidationError):  # a symlink entry is not a regular file
        service.write_file(w, OWNER, expected_version=1, path='src/link', content='x')
    with pytest.raises(ValidationError):  # nothing can be created beneath a symlink
        service.write_file(w, OWNER, expected_version=1, path='src/link/escape.py', content='x')
    with pytest.raises(ValidationError):  # ... or beneath a regular file
        service.write_file(w, OWNER, expected_version=1, path='other.py/child', content='x')
    with pytest.raises(ValidationError):  # binary content is refused in both directions
        service.replace_text(w, OWNER, expected_version=1, path='data.bin', old='x', new='y')
    with pytest.raises(ValidationError):
        service.write_file(w, OWNER, expected_version=1, path='bin.dat', content='a\x00b')
    assert service.get(w, OWNER, operation='read')['version'] == 1


def test_replace_text_requires_the_expected_number_of_matches(opened):
    service = opened[0]
    w = ws(opened)
    done = service.replace_text(w, OWNER, expected_version=0, path='src/mod.py', old='return 1', new='return 2')
    assert done['version'] == 1 and 'return 2' in [l['text'].strip() for l in service.read(w, OWNER, path='src/mod.py')['lines']]
    with pytest.raises(ValidationError) as info:
        service.replace_text(w, OWNER, expected_version=1, path='src/mod.py', old='return', new='yield')
    assert info.value.diagnostic['found'] == 2 and info.value.diagnostic['at_lines'] == [5, 11]
    assert service.replace_text(w, OWNER, expected_version=1, path='src/mod.py', old='return', new='yield', expected_occurrences=2)['version'] == 2
    with pytest.raises(ValidationError):
        service.replace_text(w, OWNER, expected_version=2, path='src/mod.py', old='absent', new='x')


def test_delete_and_rename(opened):
    service = opened[0]
    w = ws(opened)
    with pytest.raises(ValidationError):
        service.delete_path(w, OWNER, expected_version=0, path='tests')  # directory needs recursive
    service.rename_file(w, OWNER, expected_version=0, source='tests/test_a.py', destination='tests/test_renamed.py')
    paths = {e['path'] for e in service.tree(w, OWNER, path='tests')['entries']}
    assert paths == {'tests/test_renamed.py'}
    with pytest.raises(ValidationError):  # destination exists
        service.rename_file(w, OWNER, expected_version=1, source='src/foo.py', destination='other.py')
    service.delete_path(w, OWNER, expected_version=1, path='tests', recursive=True)
    assert service.tree(w, OWNER, path='tests')['entries'] == []
    diff = service.diff(w, OWNER)
    assert {(f['path'], f['status']) for f in diff['files']} == {('tests/test_a.py', 'deleted')}


def test_apply_patch_adds_modifies_and_deletes_and_explains_failures(opened):
    service = opened[0]
    w = ws(opened)
    patch = ('diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1,3 +1,4 @@\n import os\n+import sys\n \n class Alpha:\n'
             'diff --git a/src/new_mod.py b/src/new_mod.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/new_mod.py\n@@ -0,0 +1,2 @@\n+X = 1\n+Y = 2\n'
             'diff --git a/other.py b/other.py\ndeleted file mode 100644\n--- a/other.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n')
    done = service.apply_patch(w, OWNER, expected_version=0, patch=patch)
    assert done['version'] == 1 and set(done['paths']) == {'src/mod.py', 'src/new_mod.py', 'other.py'}
    assert service.read(w, OWNER, path='src/mod.py', start_line=1, end_line=2)['lines'][1]['text'] == 'import sys'
    with pytest.raises(ValidationError) as info:  # stale context
        service.apply_patch(w, OWNER, expected_version=1, patch=patch)
    assert info.value.diagnostic['stage'] == 'apply'
    with pytest.raises(ValidationError) as info:  # a miscounted hunk keeps the stage-specific diagnostics
        service.apply_patch(w, OWNER, expected_version=1, patch=patch.replace('@@ -0,0 +1,2 @@', '@@ -0,0 +1,3 @@'))
    assert info.value.diagnostic['stage'] == 'hunk_count'
    with pytest.raises(SecurityError):
        service.apply_patch(w, OWNER, expected_version=1, patch='diff --git a/.git/x b/.git/x\nnew file mode 100644\n--- /dev/null\n+++ b/.git/x\n@@ -0,0 +1 @@\n+x\n')
    assert service.get(w, OWNER, operation='read')['version'] == 1


def test_reset_and_checkpoints(opened):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='a.txt', content='one\n')
    cp = service.checkpoint(w, OWNER, expected_version=1, name='first')
    assert cp['checkpoints'] == ['first']
    service.write_file(w, OWNER, expected_version=1, path='a.txt', content='two\n')
    assert service.diff(w, OWNER, against='first')['files'][0]['path'] == 'a.txt'
    restored = service.reset(w, OWNER, expected_version=2, to='first')
    assert restored['version'] == 3 and restored['tree'] == cp['tree']  # history is kept, the version still advances
    clean = service.reset(w, OWNER, expected_version=3)
    assert clean['modified'] is False and clean['tree'] == clean['source_tree']
    with pytest.raises(ValidationError):
        service.reset(w, OWNER, expected_version=4, to='missing')
    with pytest.raises(ValidationError):
        service.checkpoint(w, OWNER, expected_version=4, name='Bad Name')
    with pytest.raises(VersionConflictError):
        service.checkpoint(w, OWNER, expected_version=0, name='stale')


def test_diff_is_complete_deterministic_pageable_and_applies_to_the_source(opened, tmp_path):
    service, _, repo, head = opened
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='tests/test_new.py', content='def test_n():\n    assert 1\n')
    service.replace_text(w, OWNER, expected_version=1, path='src/mod.py', old='return 1', new='return 2')
    full = service.diff(w, OWNER)
    assert full['files_changed'] == 2 and full['complete'] and full['patch_bytes'] > 0 and full['version'] == 2
    assert service.diff(w, OWNER)['patch_sha256'] == full['patch_sha256']
    paged = service.diff(w, OWNER, max_lines=3)
    assert paged['patch_has_more'] and paged['patch_lines_returned'] == 3 and paged['complete'] is False
    assert paged['patch_sha256'] == full['patch_sha256']  # the digest always covers the whole patch
    # round trip: the canonical patch applies to the source tree with plain git and reproduces the workspace tree
    patch = service.canonical_patch(w, full['base_tree'], full['tree'])
    scratch = tmp_path / 'rt'
    subprocess.run(['git', 'clone', '-q', str(repo), str(scratch)], check=True)
    subprocess.run(['git', '-C', str(scratch), 'checkout', '-q', head], check=True)
    subprocess.run(['git', '-C', str(scratch), 'apply', '--index', '-'], input=patch, check=True)
    tree = subprocess.run(['git', '-C', str(scratch), 'write-tree'], capture_output=True, text=True, check=True).stdout.strip()
    assert tree == full['tree']
    assert service.diff(w, OWNER, path='src/mod.py')['patch'].count('diff --git') == 1


# --- concurrency, ownership, lifetime, limits ---------------------------------------------------------------------

def test_concurrent_edits_from_one_version_never_overwrite_each_other(opened):
    service = opened[0]
    w = ws(opened)
    outcomes = []
    barrier = threading.Barrier(8)

    def attempt(n):
        barrier.wait()
        try:
            service.write_file(w, OWNER, expected_version=0, path=f'f{n}.txt', content=f'{n}\n')
            outcomes.append('ok')
        except VersionConflictError:
            outcomes.append('conflict')
    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count('ok') == 1 and outcomes.count('conflict') == 7
    record = service.get(w, OWNER, operation='read')
    assert record['version'] == 1 and len(service.diff(w, OWNER)['files']) == 1


def test_serialized_edits_all_land_when_each_uses_the_latest_version(opened):
    service = opened[0]
    w = ws(opened)
    for n in range(5):
        service.write_file(w, OWNER, expected_version=n, path=f'f{n}.txt', content=f'{n}\n')
    assert len(service.diff(w, OWNER)['files']) == 5 and service.get(w, OWNER, operation='read')['version'] == 5


def test_other_callers_and_unknown_ids_are_indistinguishable(opened):
    service = opened[0]
    w = ws(opened)
    for other, wid in (('mallory', w), (OWNER, 'ws-' + '0' * 32), (OWNER, '../etc'), (OWNER, 'ws-short')):
        with pytest.raises(ValidationError) as info:
            service.read(wid, other, path='other.py')
        assert str(info.value) == 'Unknown workspace_id.'
    with pytest.raises(ValidationError):
        service.write_file(w, 'mallory', expected_version=0, path='x', content='x')
    assert service.list_for('mallory') == [] and len(service.list_for(OWNER)) == 1


def test_scope_narrows_what_a_workspace_may_do(service, local_fetch):
    repo, sha = local_fetch
    summary = open_workspace(client_for(sha), 'owner/repo', service=service, owner=OWNER, source='branch', branch='feature',
                             expected_sha=sha, operations=['read', 'search'])
    assert service.read(summary['workspace_id'], OWNER, path='other.py')['lines']
    with pytest.raises(SecurityError) as info:
        service.write_file(summary['workspace_id'], OWNER, expected_version=0, path='x.txt', content='x')
    assert info.value.diagnostic['allowed_operations'] == ['read', 'search']
    with pytest.raises(ValidationError):
        open_workspace(client_for(sha), 'owner/repo', service=service, owner=OWNER, source='branch', branch='feature',
                       expected_sha=sha, operations=['read', 'shell'])


def test_expiry_extension_is_capped_and_expired_workspaces_are_purged(opened, clock):
    service = opened[0]
    w = ws(opened)
    created = opened[1]
    extended = service.extend(w, OWNER, 60)
    assert extended['expires_at'] == extended['created_at'] + 60 * 3600
    capped = service.extend(w, OWNER, 72)
    assert capped['expires_at'] == capped['created_at'] + 72 * 3600  # never past the absolute lifetime
    with pytest.raises(ValidationError):
        service.extend(w, OWNER, 73)
    clock.now += 73 * 3600
    with pytest.raises(ValidationError):
        service.read(w, OWNER, path='other.py')
    assert service.purge(force=True) >= 0 and not service._dir(w).exists()


def test_close_reports_unpublished_changes_and_removes_storage(opened):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='x.txt', content='x\n')
    closed = service.close(w, OWNER)
    assert closed['closed'] and closed['unpublished_changes'] is True and not service._dir(w).exists()
    with pytest.raises(ValidationError):
        service.read(w, OWNER, path='x.txt')


def test_limits_per_owner_file_size_and_changed_files(tmp_path, clock, local_fetch):
    repo, sha = local_fetch
    small = WorkspaceService(WorkspaceSettings(data_dir=str(tmp_path / 'lim'), max_per_owner=2, max_file_bytes=2048, max_changed_files=3,
                                              min_free_bytes=64 * 1024 * 1024), clock=clock)
    client = client_for(sha)
    ids = [open_workspace(client, 'owner/repo', service=small, owner=OWNER, source='branch', branch='feature', expected_sha=sha)['workspace_id'] for _ in range(2)]
    with pytest.raises(ValidationError) as info:
        open_workspace(client, 'owner/repo', service=small, owner=OWNER, source='branch', branch='feature', expected_sha=sha)
    assert info.value.diagnostic['max_per_owner'] == 2
    # another caller is not affected by this caller's quota
    assert open_workspace(client, 'owner/repo', service=small, owner='bob', source='branch', branch='feature', expected_sha=sha)
    w = ids[0]
    with pytest.raises(ValidationError):
        small.write_file(w, OWNER, expected_version=0, path='big.txt', content='x' * 4096)
    for n in range(3):
        small.write_file(w, OWNER, expected_version=n, path=f'f{n}.txt', content='x\n')
    with pytest.raises(ValidationError) as info:
        small.write_file(w, OWNER, expected_version=3, path='f3.txt', content='x\n')
    assert info.value.diagnostic['stage'] == 'limits' and small.get(w, OWNER, operation='read')['version'] == 3


def test_snapshot_is_immutable_while_the_workspace_moves_on(opened, tmp_path):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='marker.txt', content='before\n')
    taken = service.snapshot(w, OWNER, expected_version=1, destination=tmp_path / 'snap.tar', max_bytes=10_000_000)
    service.write_file(w, OWNER, expected_version=1, path='marker.txt', content='after\n')
    with tarfile.open(tmp_path / 'snap.tar') as archive:
        assert archive.extractfile('marker.txt').read() == b'before\n'
        names = archive.getnames()
    assert 'src/mod.py' in names and '.git' not in names and taken['record']['version'] == 1
    with pytest.raises(VersionConflictError):  # a stale caller cannot snapshot a tree it did not see
        service.snapshot(w, OWNER, expected_version=1, destination=tmp_path / 'stale.tar', max_bytes=10_000_000)


def test_state_survives_a_service_restart_and_half_created_debris_is_purged(opened, tmp_path, clock):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='keep.txt', content='k\n')
    restarted = WorkspaceService(service.cfg, clock=clock)
    assert restarted.read(w, OWNER, path='keep.txt')['version'] == 1
    debris = restarted.root / ('ws-' + 'a' * 32)
    debris.mkdir()
    os.utime(debris, (clock.now - 7200, clock.now - 7200))
    clock.now += 1
    restarted.purge(force=True)
    assert not debris.exists() and restarted._dir(w).exists()


def test_run_budget_is_enforced_per_workspace(tmp_path, clock, local_fetch):
    repo, sha = local_fetch
    small = WorkspaceService(WorkspaceSettings(data_dir=str(tmp_path / 'b'), max_runs=2, min_free_bytes=64 * 1024 * 1024), clock=clock)
    w = open_workspace(client_for(sha), 'owner/repo', service=small, owner=OWNER, source='branch', branch='feature', expected_sha=sha)['workspace_id']
    for n in range(2):
        small.snapshot(w, OWNER, expected_version=0, destination=tmp_path / f's{n}.tar', max_bytes=10_000_000, attach_run=f'test-{n:032x}')
    with pytest.raises(ValidationError) as info:
        small.snapshot(w, OWNER, expected_version=0, destination=tmp_path / 's3.tar', max_bytes=10_000_000, attach_run='test-' + 'f' * 32)
    assert info.value.diagnostic['max_runs'] == 2


def test_batch_read_returns_multiple_files(opened):
    service = opened[0]
    w = ws(opened)
    service.write_file(w, OWNER, expected_version=0, path='f1.txt', content='line 1\n')
    service.write_file(w, OWNER, expected_version=1, path='f2.txt', content='line 2\n')
    batch = service.batch_read(w, OWNER, paths=['f1.txt', 'f2.txt', 'nonexistent.txt'])
    assert batch['count'] == 3
    assert batch['files']['f1.txt']['lines'][0]['text'] == 'line 1'
    assert batch['files']['f2.txt']['lines'][0]['text'] == 'line 2'
    assert 'No such file' in batch['files']['nonexistent.txt']['error']
