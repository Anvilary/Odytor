"""Browser scenario schema, the sandbox origin policy, and evidence helpers (no browser needed)."""
import pytest

from odytor import browser_driver as driver
from odytor.browser_spec import MAX_STEPS, SpecError, validate_mock, validate_scenario, validate_serve, validate_step


def scenario(**over):
    base = {'steps': [{'op': 'goto', 'path': '/index.html'}, {'op': 'click', 'selector': '#go'}]}
    base.update(over)
    return base


def test_a_valid_scenario_is_normalized_with_deterministic_defaults():
    out = validate_scenario(scenario())
    assert out['viewport'] == {'width': 1280, 'height': 720} and out['locale'] == 'en-US' and out['timezone'] == 'UTC'
    assert out['color_scheme'] == 'light' and out['trace'] is False and out['mocks'] == [] and 'replay' not in out
    custom = validate_scenario(scenario(viewport={'width': 390, 'height': 844}, locale='pt-PT', timezone='Europe/Lisbon', color_scheme='dark', trace=True))
    assert custom['viewport'] == {'width': 390, 'height': 844} and custom['timezone'] == 'Europe/Lisbon' and custom['trace'] is True


@pytest.mark.parametrize('step', [
    {'op': 'evaluate', 'script': 'alert(1)'}, {'op': 'goto', 'path': 'http://evil.example/'}, {'op': 'goto', 'path': '//evil.example/x'},
    {'op': 'goto', 'path': 'index.html'}, {'op': 'goto', 'path': '/a\\b'}, {'op': 'goto'}, {'op': 'goto', 'path': '/x', 'url': 'http://e/'},
    {'op': 'click'}, {'op': 'click', 'selector': 'a\nb'}, {'op': 'click', 'selector': 'x' * 301}, {'op': 'click', 'selector': ''},
    {'op': 'fill', 'selector': '#a', 'text': 'x' * 2001}, {'op': 'fill', 'selector': '#a', 'text': 'bad\x00'},
    {'op': 'press', 'key': 'rm -rf'}, {'op': 'press', 'key': 'Control+'}, {'op': 'wait_ms', 'ms': 5001}, {'op': 'wait_ms', 'ms': -1},
    {'op': 'wait_ms', 'ms': True}, {'op': 'wait_for'}, {'op': 'wait_for', 'selector': '#a', 'text': 'b'}, {'op': 'click', 'selector': '#a', 'timeout_ms': 99},
    {'op': 'screenshot', 'name': '../x'}, {'op': 'screenshot', 'name': 'a b'}, {'op': 'screenshot', 'name': 'ok', 'format': 'gif'},
    {'op': 'assert_count', 'selector': '#a', 'count': -1}, {'op': 'assert_count', 'selector': '#a', 'count': 1, 'compare': 'ne'},
    {'op': 'assert_requests', 'path': '/api', 'count': 1, 'method': 'TRACE'}, 'click', None, 3, {'op': 5}, {}])
def test_unsafe_or_malformed_steps_are_rejected_with_a_located_message(step):
    with pytest.raises(SpecError) as info:
        validate_step(step, 4)
    assert 'steps[4]' in str(info.value)


def test_there_is_no_javascript_evaluation_upload_download_or_arbitrary_navigation():
    for op in ('evaluate', 'eval', 'run_code', 'upload', 'download', 'navigate', 'open', 'new_tab', 'cookie', 'storage', 'route', 'script'):
        with pytest.raises(SpecError):
            validate_step({'op': op}, 0)


def test_scenario_limits():
    with pytest.raises(SpecError):
        validate_scenario(scenario(steps=[]))
    with pytest.raises(SpecError):
        validate_scenario(scenario(steps=[{'op': 'wait_ms', 'ms': 1}] * (MAX_STEPS + 1)))
    shots = [{'op': 'screenshot', 'name': f's{i}'} for i in range(9)]
    with pytest.raises(SpecError):
        validate_scenario(scenario(steps=shots))
    with pytest.raises(SpecError):  # unique names, because they become artifact names
        validate_scenario(scenario(steps=[{'op': 'screenshot', 'name': 'a'}, {'op': 'screenshot', 'name': 'a'}]))
    for bad in ({'viewport': {'width': 10, 'height': 720}}, {'viewport': {'width': 1280}} and {'viewport': {'w': 1}}, {'locale': 'en_US'},
                {'timezone': '../../etc'}, {'color_scheme': 'blue'}, {'trace': 'yes'}, {'unknown': 1}, {'mocks': [{}] * 21}):
        with pytest.raises(SpecError):
            validate_scenario(scenario(**bad))


def test_replay_is_bounded_by_the_step_list():
    assert validate_scenario(scenario(replay=1))['replay'] == 1
    for bad in (2, -1, True):
        with pytest.raises(SpecError):
            validate_scenario(scenario(replay=bad))


def test_mocks_are_bounded_and_typed():
    ok = validate_mock({'path': '/api/x?ignored=1', 'json': {'a': [1, 2]}, 'status': 201}, 0)
    assert ok['path'] == '/api/x' and ok['method'] == 'GET' and ok['content_type'] == 'application/json'
    assert validate_mock({'path': '/t', 'body': 'hi', 'content_type': 'text/plain'}, 0)['body'] == 'hi'
    for bad in ({'path': '/x'}, {'path': '/x', 'json': {}, 'body': 'x'}, {'path': 'http://e/x', 'json': {}}, {'path': '/x', 'json': float('nan')},
                {'path': '/x', 'body': 'x' * 70000}, {'path': '/x', 'json': {}, 'status': 99}, {'path': '/x', 'json': {}, 'extra': 1},
                {'path': '/x', 'body': 'a', 'content_type': 'not a type'}, 'x'):
        with pytest.raises(SpecError):
            validate_mock(bad, 0)


def test_serve_policy_is_validated_and_static_roots_cannot_escape():
    assert validate_serve({'kind': 'static', 'root': 'static', 'port': 8000})['root'] == 'static'
    assert validate_serve({'kind': 'command', 'argv': ['python', '-m', 'http.server', '8000'], 'port': 8000})['argv'][0] == 'python'
    for bad in ({'kind': 'static', 'root': '../x'}, {'kind': 'static', 'root': '/etc'}, {'kind': 'static', 'root': '.git'},
                {'kind': 'static', 'root': 'a/../../b'}, {'kind': 'static', 'root': 'a\\b'}, {'kind': 'shell'}, {'kind': 'command'},
                {'kind': 'command', 'argv': []}, {'kind': 'command', 'argv': [1]}, {'kind': 'static', 'port': 80},
                {'kind': 'static', 'ready_path': 'http://x/'}, None):
        with pytest.raises(SpecError):
            validate_serve(bad)


@pytest.mark.parametrize('url,allowed', [
    ('http://127.0.0.1:8765/index.html', True), ('http://127.0.0.1:8765', True), ('ws://127.0.0.1:8765/socket', True),
    ('data:text/html,hi', True), ('blob:http://127.0.0.1:8765/abc', True), ('about:blank', True),
    ('http://127.0.0.1:9999/', False), ('http://127.0.0.1/', False), ('http://localhost:8765/', False), ('http://127.1:8765/', False),
    ('http://127.0.0.1.evil.example:8765/', False), ('http://127.0.0.1:8765@evil.example/', False), ('http://user:pw@127.0.0.1:8765/', False),
    ('https://127.0.0.1:8765/', False), ('wss://127.0.0.1:8765/', False), ('ftp://127.0.0.1:8765/', False), ('file:///etc/passwd', False),
    ('http://[::1]:8765/', False), ('http://169.254.169.254/latest/meta-data/', False), ('http://metadata.google.internal/', False),
    ('http://10.0.0.1:8765/', False), ('https://example.com/', False), ('javascript:alert(1)', False), ('chrome://settings', False),
    ('http://127.0.0.1:99999/', False), ('', False), ('//127.0.0.1:8765/', False)])
def test_origin_allowlist_admits_only_the_local_app(url, allowed):
    assert driver.origin_allowed(url, 8765) is allowed


def test_mock_matching_ignores_the_query_but_not_the_method_or_path():
    mocks = [{'method': 'GET', 'path': '/api/models'}, {'method': 'POST', 'path': '/api/models'}]
    assert driver.match_mock(mocks, 'GET', 'http://127.0.0.1:1/api/models?engine=all') is mocks[0]
    assert driver.match_mock(mocks, 'POST', 'http://127.0.0.1:1/api/models') is mocks[1]
    assert driver.match_mock(mocks, 'DELETE', 'http://127.0.0.1:1/api/models') is None
    assert driver.match_mock(mocks, 'GET', 'http://127.0.0.1:1/api/models/1') is None


def test_request_counting_uses_exact_paths_and_a_baseline_and_ignores_blocked_requests():
    log = [{'method': 'GET', 'path': '/api/m?a=1'}, {'method': 'GET', 'path': '/api/m'}, {'method': 'POST', 'path': '/api/m'},
           {'method': 'GET', 'path': '/api/m2'}, {'method': 'GET', 'path': '/api/m', 'blocked': True}]
    assert driver.count_requests(log, '/api/m') == 3 and driver.count_requests(log, '/api/m', 'GET') == 2
    assert driver.count_requests(log, '/api/m', since=2) == 1 and driver.count_requests(log, '/nope') == 0


def test_network_summary_is_bounded_and_counts_blocked_separately():
    entries = [{'method': 'GET', 'path': f'/p{i}'} for i in range(500)] + [{'method': 'GET', 'path': '[blocked] evil', 'blocked': True}]
    summary = driver.summarize_network(entries)
    assert summary['total'] == 501 and summary['blocked'] == 1 and len(summary['entries']) == 400 and summary['entries_truncated']
    assert len(summary['by_path']) == 100 and summary['by_path_truncated'] and 'GET [blocked] evil' not in summary['by_path']


def test_compare():
    assert driver.compare('eq', 2, 2) and not driver.compare('eq', 3, 2) and driver.compare('le', 1, 2) and driver.compare('ge', 3, 2)
    assert not driver.compare('le', 3, 2) and not driver.compare('ge', 1, 2)
