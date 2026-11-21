"""Workspace -> edit -> real rootless-container test, end to end through the MCP tools.

Needs podman and ODYTOR_TEST_RUNNER_IMAGE (a pinned local image), like tests/test_sandbox_integration.py.
"""
import asyncio
import contextlib
import json
import multiprocessing
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from fastmcp import Client
from unittest.mock import Mock

from odytor.config import ArtifactSettings, RunSettings, Settings, WorkspaceSettings
from odytor.profile import requirements_digest
from odytor.server import create_server
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

IMAGE = os.environ.get('ODYTOR_TEST_RUNNER_IMAGE')
pytestmark = pytest.mark.skipif(not (shutil.which('podman') and IMAGE),
                                reason='needs podman and ODYTOR_TEST_RUNNER_IMAGE (a pinned local image)')
PYTEST_PIN = os.environ.get('ODYTOR_TEST_PYTEST_PIN', 'pytest==8.3.5')
STAGE_RUNNER_COMMAND = os.environ.get('ODYTOR_TEST_STAGE_RUNNER_COMMAND')
TERMINAL = {'passed', 'failed', 'no_tests_collected', 'no_tests_executed', 'setup_error', 'timeout', 'canceled'}
SLOW_OVERLAY = ('diff --git a/tests/test_calc.py b/tests/test_calc.py\n'
                '--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n'
                '@@ -1,4 +1,6 @@\n from app.calc import add\n+import time\n \n def test_add():\n'
                '+    time.sleep(90)\n     assert add(2, 3) == 5\n')


@pytest.fixture(scope='module')
def runner_root(tmp_path_factory):
    return tmp_path_factory.mktemp('runner')


@pytest.fixture
def stack(tmp_path, monkeypatch, local_fetch, runner_root, request):
    repo, _ = local_fetch
    (repo / 'app').mkdir()
    (repo / 'app/calc.py').write_text('def add(a, b):\n    return a - b\n')  # deliberate defect
    (repo / 'tests').mkdir()
    (repo / 'tests/test_calc.py').write_text('from app.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    (repo / 'tests/conftest.py').write_text('import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n')
    (repo / 'playwright/first').mkdir(parents=True)
    (repo / 'playwright/second').mkdir()
    (repo / 'playwright/launcher.py').write_text(
        'import subprocess, sys\n'
        'sys.exit(subprocess.call(["node", "/usr/local/lib/node_modules/@playwright/test/cli.js", "test", '
        '"--config=playwright/playwright.config.cjs", *sys.argv[1:]]))\n')
    (repo / 'playwright/playwright.config.cjs').write_text('module.exports = { testDir: ".", workers: 1 };\n')
    spec = 'const { test } = require("@playwright/test");\ntest("first", async () => {});\n'
    (repo / 'playwright/first/paced.spec.cjs').write_text(
        spec + 'test("later", async () => { await new Promise(resolve => setTimeout(resolve, 7000)); });\n')
    (repo / 'playwright/second/paced.spec.cjs').write_text(spec)
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'calc with a bug')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    policy_dir = tmp_path / 'policy'
    policy_dir.mkdir()
    (policy_dir / 'req.txt').write_text(PYTEST_PIN + '\n')
    (policy_dir / 'policy.toml').write_text(
        '[repos."owner/repo"]\nprofile = "python-pytest"\ntest_dirs = ["tests"]\nrequirements = "req.txt"\n'
        '[repos."owner/repo".playwright-spec]\nspec_dirs = ["playwright"]\n'
        'specs = ["playwright/first/paced.spec.cjs", "playwright/second/paced.spec.cjs"]\n'
        'launcher = "playwright/launcher.py"\nconfig = "playwright/playwright.config.cjs"\n'
        '[repos."owner/repo".browser-playwright]\nkind = "static"\nroot = "missing-app"\n')
    monkeypatch.setenv('ODYTOR_RUNNER_IMAGE', IMAGE)
    monkeypatch.setenv('ODYTOR_RUNNER_ROOT', str(runner_root))
    client = client_for(head)
    client.session = Mock()

    @contextlib.contextmanager
    def fake_client(cfg, remote=False, *, repo=None):
        yield client

    monkeypatch.setattr('odytor.server.github_client', fake_client)
    monkeypatch.setattr('odytor.server.GitHubClient', lambda **kwargs: client)
    cfg = Settings(
        github_token='t', github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500, default_max_diff_bytes=1048576,
        allowed_repositories=('owner/repo',),
        testing=RunSettings(runner_command=tuple(json.loads(STAGE_RUNNER_COMMAND)) if STAGE_RUNNER_COMMAND else
                            (sys.executable, '-m', 'odytor.sandbox'), policies_path=str(policy_dir / 'policy.toml'),
                            data_dir=str(tmp_path / 'runs'), default_timeout=getattr(request, 'param', 120), max_timeout=300),
        artifacts=ArtifactSettings(data_dir=str(tmp_path / 'artifacts')),
        workspaces=WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024))
    server = create_server(cfg)
    server.fixture_settings = cfg
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


@pytest.fixture
def mcp_url(stack):
    server, _ = stack
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    host, port = sock.getsockname()
    app = uvicorn.Server(uvicorn.Config(server.http_app(stateless_http=True, json_response=True), log_level='error'))
    thread = threading.Thread(target=app.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not app.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app.started
        yield f'http://{host}:{port}/mcp'
    finally:
        app.should_exit = True
        thread.join(10)
        sock.close()


@pytest.mark.parametrize('profile', ['python-pytest', pytest.param('playwright-spec', marks=pytest.mark.skipif(
    not os.environ.get('ODYTOR_TEST_PLAYWRIGHT_SPEC'), reason='needs the runner image with npm @playwright/test'))])
def test_http_observes_first_completion_during_later_test(stack, mcp_url, profile):
    _, head = stack

    async def exercise():
        async with Client(mcp_url, timeout=20) as client:
            async def call(name, **kwargs):
                return (await client.call_tool(name, kwargs)).structured_content

            created = await call('workspace_create', repo='owner/repo', number=1, expected_head=head)
            wid = created['workspace_id']
            if profile == 'python-pytest':
                paced = await call('workspace_edit', workspace_id=wid, expected_version=0, operation='write',
                                   path='tests/test_paced.py', content='import time\n\ndef test_first(): pass\n'
                                   '\ndef test_later(): time.sleep(7)\n')
                selected = 'tests/test_paced.py'
                first_name, later_name = 'test_first', 'test_later'
            else:
                paced = created
                selected = 'playwright/first/paced.spec.cjs'
                first_name, later_name = 'first', 'later'
                rejected = await call('workspace_run', workspace_id=wid, expected_version=0, profile=profile,
                                      selectors=[selected, 'playwright/second/paced.spec.cjs'])
                assert 'one selector' in rejected['error']
            submitted = await call('workspace_run', workspace_id=wid, expected_version=paced['version'],
                                   profile=profile, selectors=[selected])
            rid = submitted['run_id']
            cursor, events, observed = 0, [], None
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                status = await call('test_result', run_id=rid, wait_seconds=10, after_event_id=cursor, max_events=2)
                progress = status['live_progress']
                cursor = progress['next_cursor']
                events.extend(progress['events'])
                if status['status'] == 'running' and any(t['title'].endswith(later_name) for t in progress['active_tests']):
                    first = next((ev for ev in events if ev['type'] == 'test_end' and ev['id'].endswith(first_name)), None)
                    if first and observed is None:
                        observed = time.time(), first['timestamp']
                if status['status'] in TERMINAL and not progress['has_more']:
                    break
            assert observed is not None, events
            assert status['status'] == 'passed' and status['counts']['passed'] == 2, status
            assert status['workspace']['tested_tree'] == paced['tree']
            terminal_time = next(ev['timestamp'] for ev in events if ev['type'] == 'terminal')
            assert observed[1] <= observed[0] < terminal_time
            print(json.dumps({'run_id': rid, 'profile': profile, 'tested_tree': paced['tree'], 'first_test_end': observed[1],
                              'received_while_later_test_running': observed[0], 'terminal': terminal_time}))
            await call('workspace_manage', action='close', workspace_id=wid)

    asyncio.run(exercise())


def test_http_campaign_same_key_cancel_and_release(stack, mcp_url):
    server, head = stack

    async def exercise():
        async with Client(mcp_url, timeout=20) as client:
            async def call(name, **kwargs):
                return (await client.call_tool(name, kwargs)).structured_content

            args = {'repo': 'owner/repo', 'number': 1, 'expected_head': head, 'idempotency_key': 'staging-run',
                    'profile_selectors': {'python-pytest': ['tests/test_calc.py']}, 'overlay_patch': SLOW_OVERLAY}
            one, two = await asyncio.gather(call('validation_campaign', **args),
                                            call('validation_campaign', **args, profiles=['python-pytest']))
            cid = one['campaign_id']
            assert two['campaign_id'] == cid, (one, two)
            assert bool(one.get('idempotent_replay')) != bool(two.get('idempotent_replay'))
            conflict = await call('validation_campaign', **{**args, 'overlay_patch': SLOW_OVERLAY.replace('90', '80')})
            assert conflict['diagnostic']['classification'] == 'idempotency_key_conflict'
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                status = await call('campaign_status', campaign_id=cid, wait_seconds=5)
                child = status.get('child_run', {})
                if child.get('phase') == 'executing_tests':
                    break
                assert status['status'] == 'running', status
            rid = child['run_id']
            assert child['phase'] == 'executing_tests', status
            assert status['tested_tree'] == server.test_service.get(rid)['trees']['effective_tree']
            # Disconnecting a separate long-poll client does not signal the runner.
            async with Client(mcp_url, timeout=20) as monitor:
                waiter = asyncio.create_task(monitor.call_tool('test_result', {
                    'run_id': rid, 'after_event_id': child['latest_event_id'], 'wait_seconds': 10}))
                await asyncio.sleep(0.2)
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            assert (await call('test_result', run_id=rid))['status'] == 'running'
            canceled_at = time.time()
            canceled = await call('campaign_cancel', campaign_id=cid)
            assert canceled['status'] == 'canceling'
            deadline = time.monotonic() + 55
            while time.monotonic() < deadline:
                terminal = await call('campaign_status', campaign_id=cid, wait_seconds=5)
                if terminal['status'] == 'canceled':
                    break
            assert terminal['status'] == 'canceled' and terminal['workspace_cleaned'], terminal
            assert (await call('test_result', run_id=rid))['status'] == 'canceled'
            assert server.test_service.queue_depth()['running'] == 0
            if STAGE_RUNNER_COMMAND:
                from test_ssh_runner import containers, workspaces
                assert containers() == [] and workspaces() == []
            else:
                assert gp_containers() == []
            print(json.dumps({'campaign_id': cid, 'run_id': rid, 'tested_tree': terminal['tested_tree'],
                              'cancel_requested': canceled_at, 'cleanup_observed': time.time()}))
            assert (await call('validation_campaign', **args))['status'] == 'canceled'
            assert len(server.test_service.store.records()) == 1

    asyncio.run(exercise())


def test_campaign_reconciles_after_active_control_plane_is_killed(stack, runner_root):
    server, head = stack
    args = {'repo': 'owner/repo', 'number': 1, 'expected_head': head, 'idempotency_key': 'restart-test',
            'profile_selectors': {'python-pytest': ['tests/test_calc.py']}, 'overlay_patch': SLOW_OVERLAY}
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)

    def child_main():
        sender.send(data(create_server(server.fixture_settings), 'validation_campaign', **args))
        time.sleep(150)

    child = context.Process(target=child_main)
    child.start()
    sender.close()
    try:
        assert receiver.poll(15), 'isolated control plane did not reply'
        cid = receiver.recv()['campaign_id']
        if STAGE_RUNNER_COMMAND:
            from test_ssh_runner import containers, workspaces
        else:
            containers = gp_containers

            def workspaces():
                return list((runner_root / 'jobs').glob('job-*'))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not any(name.startswith('gp-run-') for name in containers()):
            time.sleep(0.25)
        assert any(name.startswith('gp-run-') for name in containers()), 'sandbox never started'
        campaign_file = server.workspace_service.root / 'campaigns' / f'{cid}.json'
        original = json.loads(campaign_file.read_text())
        rid = original['current_run_id']
        assert rid and original['status'] == 'running'
        killed_at = time.time()
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
        assert child.exitcode == -signal.SIGKILL
        reconciled = data(server, 'campaign_status', campaign_id=cid)
        assert reconciled['status'] == 'setup_error' and reconciled['failure']['kind'] == 'service_restarted', reconciled
        assert reconciled['workspace_cleaned'] and reconciled['tested_tree'] == original['tested_tree']
        run = data(server, 'test_result', run_id=rid)
        assert run['status'] == 'setup_error' and run['failure']['kind'] == 'service_restarted', run
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and (containers() or workspaces()):
            time.sleep(0.25)
        assert containers() == [] and workspaces() == []
        assert data(server, 'validation_campaign', **args)['campaign_id'] == cid
        print(json.dumps({'campaign_id': cid, 'run_id': rid, 'tested_tree': original['tested_tree'],
                          'control_plane_killed': killed_at, 'cleanup_observed': time.time()}))
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
            child.join(10)


@pytest.mark.parametrize('stack', [10], indirect=True)
def test_repeated_outcomes_release_runtime_resources_and_retain_evidence(stack, runner_root):
    server, head = stack
    service = server.test_service
    ws_root, artifact_root = server.workspace_service.root, server.artifact_store.root

    def usage(path):
        files = [p for p in path.rglob('*') if p.is_file()]
        return {'files': len(files), 'bytes': sum(p.stat().st_size for p in files)}

    if STAGE_RUNNER_COMMAND:
        from test_ssh_runner import as_runner, containers, workspaces

        def runner_snapshot():
            out = as_runner('du -sb /home/gp-runner/.local/state/git-plus-runner/envs 2>/dev/null; '
                            'df -B1 --output=avail / | tail -1; '
                            'ps -u gp-runner -o comm= | grep -Ec "^(conmon|crun|odytor-runner)$"; '
                            'podman unshare findmnt -rn -o TARGET | grep -c /overlay/')
            cache, free, procs, mounts = out.splitlines()
            return {'cache_bytes': int(cache.split()[0]), 'free_bytes': int(free), 'processes': int(procs), 'mounts': int(mounts)}
    else:
        containers = gp_containers

        def workspaces():
            return list((runner_root / 'jobs').glob('job-*'))

        def runner_snapshot():
            return {'cache_bytes': usage(runner_root / 'envs')['bytes'], 'free_bytes': shutil.disk_usage(runner_root).free}

    def snapshot():
        return {'host_free_bytes': shutil.disk_usage(runner_root).free, 'stored_results': usage(service.store.directory),
                'workspace_files': usage(ws_root), 'artifacts': usage(artifact_root), 'spool': usage(service.spool_root),
                'runner': {**runner_snapshot(), 'containers': len(containers()), 'job_directories': len(workspaces())}}

    first = snapshot()
    evidence = []
    for _ in range(2):
        for expected in ('passed', 'setup_error', 'failed', 'timeout', 'canceled'):
            ws = data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)
            wid = ws['workspace_id']
            if expected == 'setup_error':
                submitted = data(server, 'browser_run', workspace_id=wid, expected_version=0,
                                 steps=[{'op': 'goto', 'path': '/index.html'}], wait_seconds=0)
                expected_tree = ws['tree']
            else:
                test = {'passed': 'pass', 'failed': 'print("staging-evidence " + "x" * 16000); assert False',
                        'timeout': 'time.sleep(30)', 'canceled': 'time.sleep(30)'}[expected]
                changed = data(server, 'workspace_edit', workspace_id=wid, expected_version=0, operation='write',
                               path='tests/test_selected.py', content=f'import time\n\ndef test_selected():\n    {test}\n')
                submitted = data(server, 'workspace_run', workspace_id=wid, expected_version=1,
                                 selectors=['tests/test_selected.py'])
                expected_tree = changed['tree']
            rid = submitted['run_id']
            started = time.monotonic()
            if expected == 'canceled':
                while time.monotonic() - started < 60 and not any(name.startswith('gp-run-') for name in containers()):
                    time.sleep(0.2)
                assert any(name.startswith('gp-run-') for name in containers())
                assert data(server, 'test_cancel', run_id=rid)['outcome'] == 'canceling'
                started = time.monotonic()
            terminal = wait_terminal(server, rid, timeout=90)
            assert terminal['status'] == expected and terminal['workspace']['tested_tree'] == expected_tree, terminal
            if expected == 'setup_error':
                assert terminal['failure']['kind'] == 'startup_failure'
                data(server, 'browser_close', session_id=submitted['session']['session_id'])
            if expected == 'failed':
                assert terminal['structured_outcomes_complete'] and terminal['log_complete'] is False
                artifact = next(art for art in terminal['artifacts'] if art['kind'] == 'log')
                assert artifact['tested_tree'] == expected_tree
                page = data(server, 'artifact_get', artifact_id=artifact['artifact_id'], mode='text', length=1024)
                assert page['window']['next_offset'] == 1024 and page['meta']['sha256'] == artifact['sha256']
            data(server, 'workspace_manage', action='close', workspace_id=wid)
            assert not (ws_root / wid).exists() and not containers() and not workspaces() and not usage(service.spool_root)['files']
            evidence.append({'run_id': rid, 'workspace_id': wid, 'tested_tree': expected_tree,
                             'status': expected, 'seconds_to_cleanup_observed': round(time.monotonic() - started, 3)})

    last = snapshot()
    assert last['stored_results']['files'] - first['stored_results']['files'] == 10
    assert last['runner']['containers'] == last['runner']['job_directories'] == last['spool']['files'] == 0
    if STAGE_RUNNER_COMMAND:
        assert last['runner']['processes'] == last['runner']['mounts'] == 0
    print(json.dumps({'measured_at': time.time(), 'before': first, 'after': last, 'runs': evidence,
                      'budgets': {'source_bytes': service.cfg.max_source_bytes, 'app_free_bytes': service.cfg.min_free_bytes,
                                  'workspace_bytes': server.workspace_service.cfg.max_total_bytes,
                                  'artifact_bytes': server.artifact_store.limits.max_total_bytes,
                                  'result_retention_hours': service.cfg.retention_hours}}))


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
