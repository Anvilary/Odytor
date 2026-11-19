"""Runtime capability discovery: honest states, no secrets, and a catalogue that matches what is registered."""
import asyncio
import json
import sys
import textwrap

import pytest

from odytor.capabilities import CORE_TOOLS, RUNNER_TOOLS, describe
from odytor.config import RunSettings, Settings
from odytor.jobs import JobService
from odytor.profile import RepoPolicy, requirements_digest
from odytor.server import create_server

SECRET = 'ghp_super_secret_token_value'


def settings(tmp_path=None, runner=None, **kw):
    testing = None
    if runner is not None:
        policy = tmp_path / 'policy.toml'
        policy.write_text('')
        testing = RunSettings(runner_command=tuple(runner), policies_path=str(policy), data_dir=str(tmp_path / 'data'))
    values = dict(github_token=SECRET, github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500,
                  default_max_diff_bytes=1048576, allowed_repositories=('owner/repo',), testing=testing)
    values.update(kw)
    return Settings(**values)


def fake_runner(tmp_path, body):
    script = tmp_path / 'runner.py'
    script.write_text(textwrap.dedent(body))
    return [sys.executable, str(script)]


def read_frame_then(reply):
    return f'''
import sys, json
head = int.from_bytes(sys.stdin.buffer.read(4), "big"); sys.stdin.buffer.read(head); sys.stdin.buffer.read(8)
sys.stdout.write({reply!r})
'''


def test_catalogue_matches_registered_tools(tmp_path):
    """The operations the report advertises must be exactly the tools the server registers."""
    cfg = settings(tmp_path, runner=['true'])
    server = create_server(cfg)
    registered = {t.name for t in asyncio.run(server.list_tools())}
    report = server and asyncio.run(server.call_tool('odytor_capabilities', {})).structured_content
    assert set(report['operations']) | {'odytor_capabilities'} == registered
    assert set(CORE_TOOLS) | set(RUNNER_TOOLS) <= registered


def test_unconfigured_runner_is_reported_as_unavailable_never_operational():
    report = asyncio.run(create_server(settings()).call_tool('odytor_capabilities', {'repo': 'owner/repo'})).structured_content
    run = report['capabilities'][0]
    assert run['configured'] is False and run['operational'] is None and run['available'] is False and 'not configured' in run['reason']
    assert {'test_run', 'test_result'}.isdisjoint(report['operations'])
    assert report['caller'] == {'principal': 'local', 'repository': 'owner/repo', 'repository_permitted': True, 'test_policy_present': False}


def test_report_never_contains_credentials_commands_or_paths(tmp_path):
    cfg = settings(tmp_path, runner=['ssh', '-i', '/home/secret/key', 'runner@internal-host'])
    text = json.dumps(asyncio.run(create_server(cfg).call_tool('odytor_capabilities', {'repo': 'owner/repo'})).structured_content)
    for forbidden in (SECRET, '/home/secret', 'internal-host', str(tmp_path), 'ssh'):
        assert forbidden not in text


def test_configured_but_unprobed_runner_has_unknown_operational_state(tmp_path):
    report = asyncio.run(create_server(settings(tmp_path, runner=['true'])).call_tool('odytor_capabilities', {})).structured_content
    run = report['capabilities'][0]
    assert run['configured'] is True and run['operational'] is None and run['runner']['state'] == 'not_probed'


def test_forbidden_repository_is_reported_as_not_authorized(tmp_path):
    report = asyncio.run(create_server(settings(tmp_path, runner=['true'])).call_tool('odytor_capabilities', {'repo': 'other/repo'})).structured_content
    run = report['capabilities'][0]
    assert run['authorized'] is False and run['available'] is False and 'not permitted' in run['reason']


def test_invalid_repo_is_a_validation_error(tmp_path):
    result = asyncio.run(create_server(settings(tmp_path, runner=['true'])).call_tool('odytor_capabilities', {'repo': '../x'})).structured_content
    assert 'error' in result


@pytest.mark.parametrize('reply,state,operational', [
    ('{"schema":1,"probe":{"operational":true,"limits":{"cpus":2}}}', 'ok', True),
    ('{"schema":1,"probe":{"operational":false,"image":{"ok":false}}}', 'degraded', False),
    ('{"schema":1,"setup_error":{"stage":"validation","message":"Invalid job or repository identifier."}}', 'probe_unsupported', False),
    ('not json', 'degraded', False)])
def test_runner_probe_states(tmp_path, reply, state, operational):
    command = fake_runner(tmp_path, read_frame_then(reply))
    cfg = settings(tmp_path, runner=command)
    service = JobService(cfg.testing)
    probe = service.probe_runner()
    assert probe['state'] == state
    report = describe(cfg, policies={}, service=service, remote=False, principal='local', runner_facts=probe)
    assert report['capabilities'][0]['runner']['state'] == state
    assert report['capabilities'][0]['operational'] is (True if state == 'ok' else False)  # only an observed ok is operational


def test_unreachable_runner_is_reported_and_cached(tmp_path):
    command = fake_runner(tmp_path, 'import sys; sys.exit(255)\n')
    service = JobService(settings(tmp_path, runner=command).testing)
    first = service.probe_runner()
    assert first['state'] == 'unreachable' and service.probe_runner() is first  # cached within max_age


def test_probe_requires_an_authorized_policy_for_available(tmp_path):
    cfg = settings(tmp_path, runner=['true'])
    policy = RepoPolicy('owner/repo', 'python-pytest', ('tests',), 'pytest==8\n', requirements_digest('pytest==8\n'))
    report = describe(cfg, policies={'owner/repo': policy}, service=None, remote=False, principal='local', repo='owner/repo',
                      runner_facts={'state': 'ok', 'facts': {'operational': True, 'limits': {'cpus': 2.0}}})
    run = report['capabilities'][0]
    assert run['available'] is True and run['operational'] is True and run['authorized'] is True
    assert report['limits']['runner'] == {'cpus': 2.0}
