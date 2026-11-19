"""Workspace -> edit -> real rootless-container test, end to end through the MCP tools.

Needs podman and ODYTOR_TEST_RUNNER_IMAGE (a pinned local image), like tests/test_sandbox_integration.py.
"""
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time

import pytest
from fastmcp import Client

from odytor.config import RunSettings, Settings, WorkspaceSettings
from odytor.profile import requirements_digest
from odytor.server import create_server
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

IMAGE = os.environ.get('ODYTOR_TEST_RUNNER_IMAGE')
pytestmark = pytest.mark.skipif(not (shutil.which('podman') and IMAGE),
                                reason='needs podman and ODYTOR_TEST_RUNNER_IMAGE (a pinned local image)')
PYTEST_PIN = os.environ.get('ODYTOR_TEST_PYTEST_PIN', 'pytest==8.3.5')
TERMINAL = {'passed', 'failed', 'no_tests_collected', 'no_tests_executed', 'setup_error', 'timeout', 'canceled'}


@pytest.fixture(scope='module')
def runner_root(tmp_path_factory):
    return tmp_path_factory.mktemp('runner')


@pytest.fixture
def stack(tmp_path, monkeypatch, local_fetch, runner_root):
    repo, _ = local_fetch
    (repo / 'app').mkdir()
    (repo / 'app/calc.py').write_text('def add(a, b):\n    return a - b\n')  # deliberate defect
    (repo / 'tests').mkdir()
    (repo / 'tests/test_calc.py').write_text('from app.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    (repo / 'tests/conftest.py').write_text('import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'calc with a bug')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    policy_dir = tmp_path / 'policy'
    policy_dir.mkdir()
    (policy_dir / 'req.txt').write_text(PYTEST_PIN + '\n')
    (policy_dir / 'policy.toml').write_text('[repos."owner/repo"]\nprofile = "python-pytest"\ntest_dirs = ["tests"]\nrequirements = "req.txt"\n')
    monkeypatch.setenv('ODYTOR_RUNNER_IMAGE', IMAGE)
    monkeypatch.setenv('ODYTOR_RUNNER_ROOT', str(runner_root))
    client = client_for(head)

    @contextlib.contextmanager
    def fake_client(cfg, remote=False, *, repo=None):
        yield client

    monkeypatch.setattr('odytor.server.github_client', fake_client)
    cfg = Settings(
        github_token='t', github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500, default_max_diff_bytes=1048576,
        allowed_repositories=('owner/repo',),
        testing=RunSettings(runner_command=(sys.executable, '-m', 'odytor.sandbox'), policies_path=str(policy_dir / 'policy.toml'),
                            data_dir=str(tmp_path / 'runs'), default_timeout=120, max_timeout=300),
        workspaces=WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024))
    server = create_server(cfg)
    yield server, head
    server.test_service.shutdown()


def data(server, name, **args):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args, raise_on_error=False)
    result = asyncio.run(go())
    assert not result.is_error, result
    return result.structured_content


def wait_terminal(server, run_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = data(server, 'test_result', run_id=run_id)
        if out.get('status') in TERMINAL:
            return out
        time.sleep(1)
    raise AssertionError('run did not finish')


def gp_containers():
    out = subprocess.run(['podman', 'ps', '-a', '--format', '{{.Names}}'], capture_output=True, text=True).stdout.split()
    return [n for n in out if n.startswith('gp-')]


def test_edit_test_fix_cycle_names_the_exact_tree_that_ran(stack):
    server, head = stack
    created = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)
    w = created['workspace_id']
    # 1. reproduce the defect on the untouched source revision
    first = data(server, 'workspace_run', workspace_id=w, expected_version=0)
    assert first['kind'] == 'workspace' and first['workspace']['tested_tree'] == created['tree'] and first['github_mutations'] == 'none'
    red = wait_terminal(server, first['run_id'])
    assert red['status'] == 'failed' and red['green'] is False and red['counts']['failed'] == 1, red
    assert any('assert' in (t.get('message') or '') for t in red['tests'] if t['outcome'] == 'failed')
    assert red['workspace']['source_tree'] == created['tree'] and red['currency']['tested_tree_is_current_workspace_tree'] is True
    # 2. fix the source in the scratch workspace and add a test
    fixed = data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='replace', path='app/calc.py', old='a - b', new='a + b')
    added = data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='write', path='tests/test_calc_more.py',
                 content='from app.calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n\ndef test_neg():\n    assert add(-1, -2) == -3\n')
    assert added['version'] == 2 and added['tree'] != fixed['tree']
    # a stale run request cannot test a tree the caller did not see
    assert data(server, 'workspace_run', workspace_id=w, expected_version=1)['error_type'] == 'VersionConflictError'
    # 3. green, with the evidence tied to version 2's tree
    second = data(server, 'workspace_run', workspace_id=w, expected_version=2)
    # 4. editing while it runs must not change what is tested
    data(server, 'workspace_edit', workspace_id=w, expected_version=2, operation='write', path='tests/test_calc_more.py', content='def test_broken():\n    assert False\n')
    green = wait_terminal(server, second['run_id'])
    assert green['status'] == 'passed' and green['green'] is True and green['counts']['passed'] == 3, green
    assert green['workspace']['tested_tree'] == added['tree'] and green['workspace']['workspace_version'] == 2
    assert green['currency']['tested_tree_is_current_workspace_tree'] is False and green['currency']['current_version'] == 3
    assert {t['id'] for t in green['tests']} == {'tests/test_calc.py::test_add', 'tests/test_calc_more.py::test_zero', 'tests/test_calc_more.py::test_neg'}
    # 5. a run on a closed workspace stays readable and says the workspace is gone
    data(server, 'workspace_manage', action='close', workspace_id=w)
    after = data(server, 'test_result', run_id=second['run_id'])
    assert after['currency']['workspace_state'] == 'closed_or_expired' and after['status'] == 'passed'
    assert gp_containers() == []


def test_a_workspace_with_no_runnable_tests_is_never_green(stack):
    server, head = stack
    w = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)['workspace_id']
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='delete', path='tests/test_calc.py')
    data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='write', path='tests/test_skip.py',
         content='import pytest\n\n@pytest.mark.skip\ndef test_s():\n    pass\n')
    out = wait_terminal(server, data(server, 'workspace_run', workspace_id=w, expected_version=2)['run_id'])
    assert out['status'] == 'no_tests_executed' and out['green'] is False and out['counts']['skipped'] == 1
    data(server, 'workspace_edit', workspace_id=w, expected_version=2, operation='delete', path='tests/test_skip.py')
    data(server, 'workspace_edit', workspace_id=w, expected_version=3, operation='write', path='tests/notes.md', content='no tests here\n')
    none = wait_terminal(server, data(server, 'workspace_run', workspace_id=w, expected_version=4)['run_id'])
    assert none['status'] in {'no_tests_collected', 'setup_error'} and none['green'] is False


def test_cancel_tears_down_the_running_container_and_leaves_nothing_behind(stack, runner_root):
    server, head = stack
    w = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)['workspace_id']
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='tests/test_slow.py',
         content='import subprocess, time\n\ndef test_sleep():\n    subprocess.Popen(["sleep", "300"])\n    time.sleep(300)\n')
    submitted = data(server, 'workspace_run', workspace_id=w, expected_version=1, selectors=['tests/test_slow.py'])
    run_id = submitted['run_id']
    deadline = time.time() + 180
    while time.time() < deadline and not gp_containers():  # wait until the sandbox container really exists
        time.sleep(0.5)
    assert any(n.startswith('gp-run-') for n in gp_containers()), 'the test container never started'
    cancel = data(server, 'test_cancel', run_id=run_id)
    assert cancel['outcome'] in {'canceling', 'canceled'}
    final = wait_terminal(server, run_id, timeout=60)
    assert final['status'] == 'canceled' and final['green'] is False
    deadline = time.time() + 30
    while time.time() < deadline and gp_containers():
        time.sleep(0.5)
    assert gp_containers() == [], 'the sandbox container (and its descendants) outlived the cancellation'
    jobs = runner_root / 'jobs'
    assert not jobs.exists() or [p for p in jobs.iterdir()] == [], 'the runner left its job workspace behind'
    assert data(server, 'test_cancel', run_id=run_id)['outcome'] == 'already_finished'


def test_other_callers_cannot_cancel_or_read_a_run(stack, monkeypatch):
    server, head = stack
    w = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)['workspace_id']
    run_id = data(server, 'workspace_run', workspace_id=w, expected_version=0)['run_id']
    monkeypatch.setattr('odytor.server.caller_principal', lambda cfg, remote: 'mallory')
    monkeypatch.setattr(server.test_service, 'cancel', lambda rid: (_ for _ in ()).throw(AssertionError('must not reach the service')))
    assert data(server, 'test_cancel', run_id=run_id)['error'] == 'Unknown run_id.'
