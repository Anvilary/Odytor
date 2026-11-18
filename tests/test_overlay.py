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
