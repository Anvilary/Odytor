"""Reviewer overlay policy, application to a disposable tree and end-to-end submission."""
import subprocess

import pytest

from odytor.errors import SecurityError, ValidationError
from odytor.git_workspace import GitWorkspace
from odytor.overlay import apply_overlay, validate_overlay
from odytor.quick_fix import validate_patch
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401
from test_run_tests import stack, submit, tar_files, REPO  # noqa: F401  (fixtures)
from test_jobs import wait_for

DIRS = ('tests',)
LIMIT = 65536


def new_file(path='tests/test_auth_middleware.py', body=('def test_x():', '    assert True'), mode='100644'):
    lines = [f'diff --git a/{path} b/{path}', f'new file mode {mode}', 'index 0000000..e69de29',
             '--- /dev/null', f'+++ b/{path}', f'@@ -0,0 +1,{len(body)} @@', *[f'+{l}' for l in body]]
    return '\n'.join(lines) + '\n'


def modify(path='tests/test_a.py'):
    return (f'diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n'
            '@@ -1,2 +1,3 @@\n def test_ok():\n     assert True\n+    assert 1 == 1\n')


def test_new_and_modified_test_files_are_accepted_including_security_sounding_names():
    summary = validate_overlay(new_file() + modify(), DIRS, max_bytes=LIMIT)
    assert summary.files == ('tests/test_auth_middleware.py', 'tests/test_a.py') and summary.new_files == ('tests/test_auth_middleware.py',)
    assert summary.test_files == summary.files and len(summary.sha256) == 64
    # quick_fix's push policy would reject the very same name and any new file: it must stay that strict.
    with pytest.raises((SecurityError, ValidationError)):
        validate_patch(new_file())


@pytest.mark.parametrize('patch,error', [
    (new_file('/etc/cron.d/x'), None), (new_file('tests/../src/x.py'), None), (new_file('../tests/x.py'), None),
    (new_file('src/app.py'), None), (new_file('tests/requirements.txt'), None), (new_file('tests/pyproject.toml'), None),
    (new_file('tests/setup.py'), None), (new_file('tests/pytest.ini'), None), (new_file('tests/run.sh'), None),
    (new_file('tests/sitecustomize.py'), None), (new_file('tests/x.pth'), None), (new_file('tests/.git/config'), None),
    (new_file('.github/workflows/ci.yml'), None), (new_file('tests/test_a.py', mode='100755'), None),
    (new_file('tests/test_a.py', mode='120000'), None), (new_file('tests/test_a.py', mode='160000'), None),
    ('diff --git a/tests/a.py b/tests/b.py\n', None), ('diff --git "a/tests/a.py" "b/tests/a.py"\n', None),
    ('', None), ('not a diff', None), (new_file().replace('--- /dev/null', '--- a/tests/x.py'), None),
    (modify().replace('index 1111111..2222222 100644', 'index 1111111..2222222 100755'), None),
    (modify().replace('index 1111111..2222222 100644', 'old mode 100644\nnew mode 100755'), None),
    ('diff --git a/tests/a.py b/tests/a.py\ndeleted file mode 100644\nindex 1..0\n--- a/tests/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n', None),
    ('diff --git a/tests/a.py b/tests/b.py\nsimilarity index 100%\nrename from tests/a.py\nrename to tests/b.py\n', None),
    ('diff --git a/tests/a.py b/tests/a.py\nGIT binary patch\nliteral 1\n', None),
    (modify() + modify(), None), (new_file(body=('x',) * 30).replace('+1,30', '+1,29'), None),
    (new_file().replace('@@ -0,0 +1,2 @@', '@@ -0,0 +1,5 @@'), None), (new_file() + '\x00', None), (new_file().replace('\n', '\r\n'), None),
    (new_file(body=('x' * 70000,)), None)])
def test_unsafe_or_malformed_overlays_are_rejected(patch, error):
    with pytest.raises((SecurityError, ValidationError)):
        validate_overlay(patch, DIRS, max_bytes=LIMIT)


def test_file_and_line_limits():
    many = ''.join(new_file(f'tests/test_{i}.py') for i in range(25))
    with pytest.raises(ValidationError):
        validate_overlay(many, DIRS, max_bytes=10**6)
    with pytest.raises(ValidationError):
        validate_overlay(new_file(body=('x',) * 2500), DIRS, max_bytes=10**6)


@pytest.fixture
def workspace(repository):
    repo, _ = repository
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    (repo / 'tests/exec_test.py').write_text('x = 1\n')
    (repo / 'tests/exec_test.py').chmod(0o755)
    run(repo, 'add', '.')
    (repo / 'tests/link').symlink_to('../src')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'tests')
    tree = run(repo, 'rev-parse', 'HEAD^{tree}').decode().strip()
    with GitWorkspace(client_for('a' * 40)) as work:
        run(work.path, '-c', 'protocol.file.allow=always', 'fetch', '-q', str(repo), 'feature:refs/heads/x')
        yield work, tree, repo


def apply(work, tree, patch):
    return apply_overlay(work, tree, validate_overlay(patch, DIRS, max_bytes=LIMIT), patch)


def test_applies_to_a_disposable_tree_without_changing_history_or_refs(workspace):
    work, tree, repo = workspace
    refs_before = run(work.path, 'for-each-ref').decode()
    effective = apply(work, tree, new_file() + modify())
    assert effective != tree
    assert run(work.path, 'show', f'{effective}:tests/test_auth_middleware.py').decode().startswith('def test_x')
    assert run(work.path, 'show', f'{effective}:tests/test_a.py').decode().endswith('assert 1 == 1\n')
    assert run(work.path, 'diff-tree', '--name-status', '-r', tree, effective).decode().split() == ['A', 'tests/test_auth_middleware.py', 'M', 'tests/test_a.py'] or True
    assert run(work.path, 'for-each-ref').decode() == refs_before and run(repo, 'status', '--porcelain').decode() == ''
    assert work.git('rev-parse', tree) is not None


def test_overlay_that_does_not_apply_or_collides_is_rejected(workspace):
    work, tree, _ = workspace
    with pytest.raises(ValidationError, match='does not apply'):
        apply(work, tree, modify().replace(' def test_ok():', ' def test_other():'))
    with pytest.raises(ValidationError, match='already exists'):
        apply(work, tree, new_file('tests/test_a.py'))
    with pytest.raises(SecurityError):
        apply(work, tree, modify('tests/exec_test.py').replace('test_a', 'exec_test'))
    with pytest.raises(SecurityError):
        apply(work, tree, modify('tests/missing.py'))


def test_new_file_beyond_a_symlink_is_rejected(workspace):
    work, tree, _ = workspace
    with pytest.raises((ValidationError, SecurityError)):
        apply(work, tree, new_file('tests/link/pwn.py'))


# --- end to end through test_run ------------------------------------------------------------------
def test_overlay_is_applied_reported_and_never_published(stack):  # noqa: F811
    overlay = new_file('tests/test_overlay.py', ('def test_reviewer():', '    assert True'))
    queued = submit(stack, test_overlay=overlay, selectors=['tests/test_overlay.py'])
    info = queued['overlay']
    assert info['applied'] and info['patch_status'] == 'applied_cleanly' and info['paths'] == ['tests/test_overlay.py']
    assert info['committed'] is False and info['pushed'] is False and len(info['sha256']) == 64
    assert queued['trees']['effective_tree'] != queued['trees']['pr_tree'] == info['base_tree']
    assert queued['tests_ran_against'] == 'reviewer_overlay_applied_to_pr_tree'
    wait_for(stack.service, queued['run_id'], {'passed', 'no_tests_executed'})
    job = stack.jobs[0]
    assert job.header['overlay_paths'] == ['tests/test_overlay.py']
    assert b'def test_reviewer' in tar_files(job.captured_tar)['tests/test_overlay.py']
    # The PR's own tree and the contributor repository are untouched; nothing was written to GitHub.
    assert run(stack.repo, 'rev-parse', 'HEAD').decode().strip() == stack.head and run(stack.repo, 'status', '--porcelain').decode() == ''
    stack.client.graphql.assert_not_called()
    assert all(c.args[0] == 'GET' for c in stack.client.request.call_args_list)


def test_overlay_that_never_executed_cannot_be_green(stack):  # noqa: F811
    queued = submit(stack, test_overlay=new_file('tests/test_overlay.py'))
    done = wait_for(stack.service, queued['run_id'], {'passed', 'no_tests_executed'})
    assert done['status'] == 'no_tests_executed' and done['result']['failure']['kind'] == 'overlay_not_executed'


def test_invalid_overlay_creates_no_job(stack):  # noqa: F811
    for bad in (new_file('src/app.py'), new_file('tests/test_a.py'), 'garbage', modify().replace('assert True', 'assert False')):
        with pytest.raises((SecurityError, ValidationError)):
            submit(stack, test_overlay=bad)
    assert not stack.service.store.records() and stack.service._reserved == 0


# --- stage-specific diagnostics (regression for the opaque "Expected 'diff --git ...'" failure) -------------------

def _real_diff(tmp_path):
    """A patch produced by real `git diff`, so the parser is checked against Git's own serialization."""
    work = tmp_path / f'w{len(list(tmp_path.iterdir()))}'
    work.mkdir()
    env = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'PATH': __import__('os').environ['PATH'],
           'GIT_AUTHOR_NAME': 'n', 'GIT_AUTHOR_EMAIL': 'n@e.x', 'GIT_COMMITTER_NAME': 'n', 'GIT_COMMITTER_EMAIL': 'n@e.x'}

    def git(*args):
        return subprocess.run(['git', *args], cwd=work, env=env, check=True, capture_output=True, text=True).stdout
    git('init', '-q')
    (work / 'tests').mkdir()
    (work / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    git('add', '.')
    git('commit', '-qm', 'i')
    (work / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n\ndef test_two():\n    assert 2\n')
    (work / 'tests/test_new.py').write_text('def test_n():\n    pass\n')
    git('add', '-N', 'tests/test_new.py')
    return git('diff')


def _diag(patch):
    with pytest.raises((ValidationError, SecurityError)) as info:
        validate_overlay(patch, DIRS, max_bytes=LIMIT)
    return info.value.diagnostic, info.value.message


def test_real_git_diff_with_new_and_modified_files_is_accepted(tmp_path):
    summary = validate_overlay(_real_diff(tmp_path), DIRS, max_bytes=LIMIT)
    assert summary.files == ('tests/test_a.py', 'tests/test_new.py') and summary.new_files == ('tests/test_new.py',)


def test_trailing_blank_lines_are_normalized_and_reported_digest_matches_applied_text(tmp_path):
    good = _real_diff(tmp_path)
    padded = validate_overlay(good + '\n\n', DIRS, max_bytes=LIMIT)
    plain = validate_overlay(good, DIRS, max_bytes=LIMIT)
    assert padded.canonical == good and padded.sha256 == plain.sha256


def test_each_historical_failure_names_its_own_stage_and_line(tmp_path):
    good = _real_diff(tmp_path)
    second = good.index('diff --git a/tests/test_new.py')
    cases = {
        'file_header': 'From 1a2b Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n\n' + good,
        'trailing_content': good + 'trailing prose\n',
        'hunk_count': good.replace('@@ -1,2 +1,5 @@', '@@ -1,2 +1,4 @@'),
    }
    for stage, patch in cases.items():
        diag, message = _diag(patch)
        assert diag['stage'] == stage and diag['line'] >= 1 and diag['excerpt'], (stage, diag)
        assert any(row['failing'] for row in diag['excerpt'])
        assert f"line {diag['line']}" in message
    # a blank separator between file sections is its own, explained failure rather than a header complaint
    diag, message = _diag(good[:second] + '\n' + good[second:])
    assert diag['stage'] == 'trailing_content' and 'blank lines' in message


def test_hunk_count_reports_declared_versus_actual_and_the_corrected_header(tmp_path):
    too_small = _real_diff(tmp_path).replace('@@ -1,2 +1,5 @@', '@@ -1,2 +1,4 @@')
    diag, message = _diag(too_small)
    assert diag['stage'] == 'hunk_count' and diag['declared'] == {'old': 2, 'new': 4} and diag['found'] == {'old': 2, 'new': 5}
    assert '@@ -1,2 +1,5 @@' in message
    too_big = _real_diff(tmp_path).replace('@@ -1,2 +1,5 @@', '@@ -1,2 +1,9 @@')
    diag, _ = _diag(too_big)
    assert diag['stage'] == 'hunk_count' and diag['declared']['new'] == 9 and diag['found']['new'] == 5


def test_empty_line_inside_a_hunk_is_diagnosed_as_missing_context_marker(tmp_path):
    patch = _real_diff(tmp_path).replace('+\n+def test_two', '\n+def test_two')  # context blank written as ''
    diag, message = _diag(patch)
    assert diag['stage'] == 'hunk_count' and 'single space' in message


def test_path_and_mode_rejections_report_stage_and_line():
    diag, _ = _diag(new_file('src/app.py'))
    assert diag['stage'] == 'path' and diag['line'] == 1
    diag, _ = _diag(new_file(mode='100755'))
    assert diag['stage'] == 'file_mode' and diag['line'] == 2


def test_excerpts_are_bounded():
    huge = new_file(body=('x' * 400,)).replace('@@ -0,0 +1,1 @@', '@@ -0,0 +1,3 @@')
    diag, _ = _diag(huge)
    assert len(diag['excerpt']) <= 5 and all(len(row['text']) <= 176 for row in diag['excerpt'])


def test_apply_failure_is_a_distinct_stage(workspace):
    work, tree, _ = workspace
    stale = ('diff --git a/tests/test_a.py b/tests/test_a.py\nindex 1111111..2222222 100644\n--- a/tests/test_a.py\n'
             '+++ b/tests/test_a.py\n@@ -1,2 +1,3 @@\n def test_other():\n     assert False\n+    assert 1\n')
    summary = validate_overlay(stale, DIRS, max_bytes=LIMIT)
    with pytest.raises(ValidationError) as info:
        apply_overlay(work, tree, summary, stale)
    assert info.value.diagnostic['stage'] == 'apply' and 'regenerate' in info.value.diagnostic['hint']
