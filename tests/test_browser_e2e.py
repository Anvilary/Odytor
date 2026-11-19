"""Real Chromium inside the real rootless sandbox, driven through the MCP tools.

Needs podman and ODYTOR_TEST_BROWSER_IMAGE: a pinned local image built from deploy/runner/image/Containerfile
(python, node and Chromium). Skipped, loudly, otherwise. Nothing here is mocked at the sandbox boundary.
"""
import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

from odytor.config import RunSettings, Settings, WorkspaceSettings
from odytor.server import create_server
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

IMAGE = os.environ.get('ODYTOR_TEST_BROWSER_IMAGE')
pytestmark = pytest.mark.skipif(not (shutil.which('podman') and IMAGE),
                                reason='needs podman and ODYTOR_TEST_BROWSER_IMAGE (a pinned local runner image with Chromium)')
FIXTURE = Path(__file__).parent / 'fixtures' / 'browser_app'
TERMINAL = {'passed', 'failed', 'no_tests_collected', 'no_tests_executed', 'setup_error', 'timeout', 'canceled', 'completed'}
MODELS = [{'name': 'alpha', 'score': 3, 'engine': 'llama'}, {'name': 'beta', 'score': 9, 'engine': 'vllm'}, {'name': 'gamma', 'score': 5, 'engine': 'llama'}]
MOCK = {'path': '/api/models', 'json': MODELS}
SORT_SCENARIO = [
    {'op': 'goto', 'path': '/index.html'}, {'op': 'assert_count', 'selector': '#list li', 'count': 3},
    {'op': 'screenshot', 'name': 'initial'}, {'op': 'reset_network_counts'},
    {'op': 'select', 'selector': '#sort', 'value': 'name'}, {'op': 'assert_text', 'selector': '#list li >> nth=0', 'text': 'alpha'},
    {'op': 'assert_requests', 'path': '/api/models', 'count': 0}, {'op': 'screenshot', 'name': 'sorted'}]


@pytest.fixture(scope='module')
def runner_root(tmp_path_factory):
    return tmp_path_factory.mktemp('browser-runner')


@pytest.fixture
def stack(tmp_path, monkeypatch, local_fetch, runner_root):
    repo, _ = local_fetch
    shutil.copytree(FIXTURE / 'static', repo / 'static')
    (repo / 'tests').mkdir()
    (repo / 'tests/add.test.mjs').write_text("import test from 'node:test';\nimport assert from 'node:assert/strict';\ntest('adds', () => assert.equal(1 + 1, 2));\n"
                                             "test('subtracts', () => assert.equal(3 - 1, 2));\n")
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'fixture app')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    policy_dir = tmp_path / 'policy'
    policy_dir.mkdir()
    (policy_dir / 'policy.toml').write_text(
        '[repos."owner/repo"]\nprofile = "node-test"\ntest_dirs = ["tests"]\n'
        '[repos."owner/repo".node-test]\ntest_dirs = ["tests"]\n'
        '[repos."owner/repo".browser-playwright]\nkind = "static"\nroot = "static"\nport = 8765\nready_path = "/index.html"\n')
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
        workspaces=WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024),
        artifacts=__import__('odytor.config', fromlist=['ArtifactSettings']).ArtifactSettings(data_dir=str(tmp_path / 'art')))
    server = create_server(cfg)
    yield server, head
    server.test_service.shutdown()


def call(server, name, **args):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args, raise_on_error=False)
    return asyncio.run(go())


def data(server, name, **args):
    result = call(server, name, **args)
    assert not result.is_error, result
    return result.structured_content


def open_workspace(server, head):
    return data(server, 'workspace_create', repo='owner/repo', number=1, expected_head=head)


def images(result):
    return [c for c in result.content if getattr(c, 'type', None) == 'image']


def containers():
    out = subprocess.run(['podman', 'ps', '-a', '--format', '{{.Names}}'], capture_output=True, text=True).stdout.split()
    return [n for n in out if n.startswith('gp-')]


def test_runner_probe_observes_the_browser_profile_from_the_image_contents(stack, runner_root):
    server, _ = stack
    probe = server.test_service.probe_runner()
    facts = probe['facts']
    assert facts['image']['tools'] == {'python': True, 'node': True, 'playwright': True, 'chromium': True}
    assert facts['profiles']['browser-playwright']['installed'] is True and facts['profiles']['node-test']['installed'] is True
    report = data(server, 'odytor_capabilities', repo='owner/repo', probe_runner=True)
    profiles = {p['name']: p for p in report['execution_profiles']}
    assert profiles['browser-playwright']['installed_on_runner'] is True and profiles['browser-playwright']['enabled_for_repo'] is True
    assert profiles['python-pytest']['enabled_for_repo'] is False  # the policy did not enable it for this repository


def test_browser_run_delivers_native_images_exact_provenance_and_network_evidence(stack):
    server, head = stack
    created = open_workspace(server, head)
    w = created['workspace_id']
    result = call(server, 'browser_run', workspace_id=w, expected_version=0, steps=SORT_SCENARIO, mocks=[MOCK], trace=True)
    assert not result.is_error
    out = result.structured_content
    assert out['status'] == 'passed' and out['green'] is True and out['kind'] == 'browser', out
    assert out['workspace']['tested_tree'] == created['tree'] and out['workspace']['workspace_version'] == 0
    assert out['counts']['assertions'] == 3 and out['counts']['failed'] == 0 and out['browser']['name'] == 'chromium'
    assert out['browser']['viewport'] == {'width': 1280, 'height': 720} and out['browser']['timezone'] == 'UTC'
    # network evidence: the data was fetched once, on load, and sorting did not refetch it
    assert out['network']['by_path']['GET /api/models'] == 1 and out['network']['blocked'] == 0 and out['network']['mocked'] == 1
    assert any('aria' in s and 'listitem: alpha' in s['aria'] for s in out['snapshots']) and out['console']['errors'] == 0
    # screenshots arrive as native MCP image content, never as base64 inside the JSON text
    shots = images(result)
    assert len(shots) == 2 and all(s.mime_type == 'image/png' for s in shots)
    assert base64.b64decode(shots[0].data).startswith(b'\x89PNG\r\n\x1a\n')
    text = next(c.text for c in result.content if c.type == 'text')
    assert shots[0].data not in text and 'iVBOR' not in text
    # artifacts carry integrity and provenance, and can be fetched again by id
    by_name = {a['name']: a for a in out['artifacts'] if 'artifact_id' in a}
    assert {'initial.png', 'sorted.png', 'trace.zip'} <= by_name.keys()
    sorted_png = by_name['sorted.png']
    assert sorted_png['tested_tree'] == created['tree'] and sorted_png['run_id'] == out['run_id'] and len(sorted_png['sha256']) == 64
    again = call(server, 'artifact_get', artifact_id=sorted_png['artifact_id'])
    assert images(again)[0].data == shots[1].data
    trace = data(server, 'artifact_get', artifact_id=by_name['trace.zip']['artifact_id'])
    assert trace['meta']['delivery'] == 'base64_chunks' and base64.b64decode(trace['base64'])[:2] == b'PK'
    assert 'not text' in data(server, 'artifact_get', artifact_id=by_name['trace.zip']['artifact_id'], mode='text')['error']
    assert containers() == []


def test_a_deliberate_regression_is_detected_with_evidence_and_the_fix_turns_it_green(stack):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    broken = data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='replace', path='static/app.js',
                  old='if (cache) render(cache);\n  else fetchModels();', new='fetchModels();')
    assert broken['version'] == 1
    red = call(server, 'browser_run', workspace_id=w, expected_version=1, steps=SORT_SCENARIO, mocks=[MOCK])
    out = red.structured_content
    assert out['status'] == 'failed' and out['failure']['kind'] == 'assertion_failed' and out['green'] is False, out
    assert 'assert_requests' in out['failure']['message'] and 'observed 1' in out['failure']['message']
    assert out['network']['by_path']['GET /api/models'] == 2  # the regression, visible as a second fetch
    failing = next(s for s in out['steps'] if s['ok'] is False)
    assert failing['op'] == 'assert_requests' and failing['assertion'] is True
    assert any(a['name'] == 'failure.png' for a in out['artifacts'] if 'artifact_id' in a) and len(images(red)) >= 1  # visual evidence of the failure
    fixed = data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='replace', path='static/app.js',
                 old='fetchModels();\n});\nengine.addEventListener', new='if (cache) render(cache);\n  else fetchModels();\n});\nengine.addEventListener')
    green = data(server, 'browser_run', workspace_id=w, expected_version=fixed['version'], steps=SORT_SCENARIO, mocks=[MOCK])
    assert green['status'] == 'passed' and green['workspace']['tested_tree'] == fixed['tree'] and green['workspace']['tested_tree'] != out['workspace']['tested_tree']


def test_sessions_replay_history_and_only_successful_steps_join_it(stack):
    server, head = stack
    created = open_workspace(server, head)
    w = created['workspace_id']
    first = data(server, 'browser_run', workspace_id=w, expected_version=0, mocks=[MOCK],
                 steps=[{'op': 'goto', 'path': '/index.html'}, {'op': 'assert_count', 'selector': '#list li', 'count': 3}])
    sid = first['session']['session_id']
    assert first['status'] == 'passed' and first['session']['steps_in_history'] == 2 and first['replayed_steps'] == 0
    second = data(server, 'browser_run', workspace_id=w, expected_version=0, session_id=sid,
                  steps=[{'op': 'select', 'selector': '#sort', 'value': 'name'}, {'op': 'assert_text', 'selector': '#list li >> nth=0', 'text': 'alpha'},
                         {'op': 'screenshot', 'name': 'step-two'}])
    assert second['status'] == 'passed' and second['replayed_steps'] == 2 and second['session']['steps_in_history'] == 5
    assert second['network']['total'] == 0  # evidence covers only the new steps: the replay's requests are not attributed to them
    assert [s['op'] for s in second['steps'] if s.get('replayed')] == ['goto', 'assert_count'] and not any(
        a['name'] == 'initial.png' for a in second['artifacts'] if 'artifact_id' in a)
    failed = data(server, 'browser_run', workspace_id=w, expected_version=0, session_id=sid,
                  steps=[{'op': 'click', 'selector': '#does-not-exist', 'timeout_ms': 500}])
    assert failed['status'] == 'failed' and failed['failure']['kind'] == 'step_failed'
    assert data(server, 'browser_run', workspace_id=w, expected_version=0, session_id=sid,
                steps=[{'op': 'assert_visible', 'selector': '#list'}])['session']['steps_in_history'] == 6  # the failed step never joined the history
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='static/extra.txt', content='x\n')
    stale = data(server, 'browser_run', workspace_id=w, expected_version=1, session_id=sid, steps=[{'op': 'assert_visible', 'selector': '#list'}])
    assert 'opened on workspace version 0' in stale['error']  # a session tests exactly one tree
    assert data(server, 'browser_close', session_id=sid)['closed'] is True
    assert 'Unknown session_id' in data(server, 'browser_run', workspace_id=w, expected_version=0, session_id=sid, steps=[{'op': 'wait_ms', 'ms': 1}])['error']


def test_exploration_without_assertions_is_not_a_validation_result(stack):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    out = data(server, 'browser_run', workspace_id=w, expected_version=0, mocks=[MOCK], steps=[{'op': 'goto', 'path': '/index.html'}, {'op': 'snapshot'}])
    assert out['status'] == 'completed' and out['green'] is False and out['failure']['kind'] == 'no_assertions'
    assert out['snapshots'] and 'Model fit' in out['snapshots'][-1]['title']


def test_everything_that_leaves_the_local_origin_is_blocked_and_reported(stack):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    out = data(server, 'browser_run', workspace_id=w, expected_version=0,
               steps=[{'op': 'goto', 'path': '/external.html'}, {'op': 'wait_ms', 'ms': 1500}, {'op': 'assert_visible', 'selector': '#out'}, {'op': 'snapshot', 'selector': '#out'}])
    assert out['status'] == 'passed'
    results = json.loads(next(s for s in out['snapshots'] if s['selector'] == '#out')['text'])
    assert results['same_origin'] == 'ok:200'
    for attempt in ('metadata', 'internet', 'localhost_other_port', 'websocket'):
        assert results[attempt].startswith('blocked:'), (attempt, results)
    # Chromium is told to refuse service workers: register() resolves without installing anything and warns about it
    assert any('Service Worker registration blocked' in m['text'] for m in out['console']['messages'])
    blocked = [e for e in out['network']['entries'] if e['blocked']]
    assert out['network']['blocked'] >= 3 and any('169.254.169.254' in e['path'] for e in blocked) and any('example.com' in e['path'] for e in blocked)
    assert not any('example.com' in e['path'] for e in out['network']['entries'] if not e['blocked'])


def test_startup_failure_and_invalid_scenarios_are_distinct_from_test_failures(stack, tmp_path):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='delete', path='static', recursive=True)
    out = data(server, 'browser_run', workspace_id=w, expected_version=1, steps=[{'op': 'goto', 'path': '/index.html'}])
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'startup_failure' and 'static root' in out['failure']['message']
    bad = data(server, 'browser_run', workspace_id=w, expected_version=1, steps=[{'op': 'goto', 'path': 'http://evil.example/'}])
    assert 'steps[0].path' in bad['error'] and 'run_id' not in bad
    assert 'error' in data(server, 'browser_run', workspace_id=w, expected_version=1, steps=[{'op': 'evaluate', 'script': 'document.cookie'}])


def test_cancelling_a_browser_run_removes_the_container_and_all_descendants(stack, runner_root):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    slow = [{'op': 'goto', 'path': '/index.html'}] + [{'op': 'wait_ms', 'ms': 5000}] * 24
    submitted = data(server, 'browser_run', workspace_id=w, expected_version=0, mocks=[MOCK], steps=slow, wait_seconds=0)
    run_id = submitted['run_id']
    deadline = time.time() + 180
    while time.time() < deadline and not any(n.startswith('gp-run-') for n in containers()):
        time.sleep(0.5)
    assert any(n.startswith('gp-run-') for n in containers()), 'the browser container never started'
    time.sleep(3)  # let Chromium and its helper processes spawn
    assert data(server, 'test_cancel', run_id=run_id)['outcome'] in {'canceling', 'canceled'}
    deadline = time.time() + 60
    final = {}
    while time.time() < deadline:
        final = data(server, 'test_result', run_id=run_id)
        if final.get('status') in TERMINAL:
            break
        time.sleep(1)
    assert final['status'] == 'canceled' and final['green'] is False
    deadline = time.time() + 30
    while time.time() < deadline and containers():
        time.sleep(0.5)
    assert containers() == []
    # Only the sandbox's own browser is checked: its binary lives under /ms-playwright inside the image, which no host browser uses.
    leftovers = subprocess.run(['pgrep', '-f', '^/ms-playwright/'], capture_output=True, text=True).stdout.split()
    assert leftovers == [], f'sandbox browser processes survived the cancellation: {leftovers}'
    jobs = runner_root / 'jobs'
    assert not jobs.exists() or list(jobs.iterdir()) == []
    session_id = submitted['session']['session_id']
    # the canceled run never joined the history
    assert data(server, 'browser_run', workspace_id=w, expected_version=0, session_id=session_id, steps=[{'op': 'goto', 'path': '/index.html'}, {'op': 'assert_visible', 'selector': 'h1'}],
                wait_seconds=60)['session']['steps_in_history'] == 2


def test_node_test_profile_executes_real_tests_and_never_greens_zero_or_skipped(stack):
    server, head = stack
    w = open_workspace(server, head)['workspace_id']

    def finish(run_id):
        for _ in range(240):
            out = data(server, 'test_result', run_id=run_id)
            if out.get('status') in TERMINAL:
                return out
            time.sleep(1)
        raise AssertionError('run did not finish')

    green = finish(data(server, 'workspace_run', workspace_id=w, expected_version=0, profile='node-test')['run_id'])
    assert green['status'] == 'passed' and green['counts']['passed'] == 2 and green['profile'] == 'node-test' and green['green'] is True, green
    assert {t['id'] for t in green['tests']} == {'adds', 'subtracts'} and green['workspace']['tested_tree']
    data(server, 'workspace_edit', workspace_id=w, expected_version=0, operation='write', path='tests/add.test.mjs',
         content="import test from 'node:test';\nimport assert from 'node:assert/strict';\ntest('adds', () => assert.equal(1 + 1, 3));\n")
    red = finish(data(server, 'workspace_run', workspace_id=w, expected_version=1, profile='node-test')['run_id'])
    assert red['status'] == 'failed' and red['counts']['failed'] == 1 and red['green'] is False
    assert any(t['outcome'] == 'failed' and t['message'] for t in red['tests'])
    data(server, 'workspace_edit', workspace_id=w, expected_version=1, operation='write', path='tests/add.test.mjs',
         content="import test from 'node:test';\ntest.skip('later', () => {});\n")
    skipped = finish(data(server, 'workspace_run', workspace_id=w, expected_version=2, profile='node-test')['run_id'])
    assert skipped['status'] == 'no_tests_executed' and skipped['green'] is False
    data(server, 'workspace_edit', workspace_id=w, expected_version=2, operation='write', path='tests/add.test.mjs', content='// no tests in this file\n')
    empty = finish(data(server, 'workspace_run', workspace_id=w, expected_version=3, profile='node-test')['run_id'])
    assert empty['status'] == 'no_tests_collected' and empty['green'] is False
    data(server, 'workspace_edit', workspace_id=w, expected_version=3, operation='write', path='tests/add.test.mjs', content='this is ( not javascript\n')
    broken = finish(data(server, 'workspace_run', workspace_id=w, expected_version=4, profile='node-test')['run_id'])
    assert broken['status'] in {'failed', 'setup_error'} and broken['green'] is False
    bad_selector = data(server, 'workspace_run', workspace_id=w, expected_version=4, profile='node-test', selectors=['../../etc/passwd'])
    assert 'error' in bad_selector
    assert 'error' in data(server, 'workspace_run', workspace_id=w, expected_version=4, profile='python-pytest')  # not enabled by this policy
    assert 'browser_run' in data(server, 'workspace_run', workspace_id=w, expected_version=4, profile='browser-playwright')['error']
    assert containers() == []


def test_a_delayed_mock_is_asynchronous_and_wait_requests_is_race_free(stack):
    """Regression: mock delay once used time.sleep inside the route handler, freezing the driver for the whole delay."""
    server, head = stack
    w = open_workspace(server, head)['workspace_id']
    slow = [dict(MOCK, delay_ms=4000)]
    out = data(server, 'browser_run', workspace_id=w, expected_version=0, mocks=slow, steps=[
        {'op': 'goto', 'path': '/index.html', 'wait_until': 'domcontentloaded'},
        {'op': 'wait_requests', 'path': '/api/models', 'count': 1, 'timeout_ms': 8000},
        {'op': 'assert_count', 'selector': '#list li', 'count': 0},                 # the slow response has not landed yet
        {'op': 'select', 'selector': '#sort', 'value': 'name'},                      # the sort change happens while the fetch is in flight
        {'op': 'wait_for', 'selector': '#list li >> nth=0', 'timeout_ms': 9000},
        {'op': 'assert_count', 'selector': '#list li', 'count': 3}])
    assert out['status'] == 'passed', out
    waited = next(s for s in out['steps'] if s['op'] == 'wait_requests')
    assert waited['ms'] < 2500, 'the driver was frozen for the duration of the mock delay'
    timeout = data(server, 'browser_run', workspace_id=w, expected_version=0, mocks=[MOCK], steps=[
        {'op': 'goto', 'path': '/index.html'}, {'op': 'wait_requests', 'path': '/api/never', 'count': 1, 'timeout_ms': 600}])
    assert timeout['status'] == 'failed' and 'were issued within 600 ms' in timeout['failure']['message']
