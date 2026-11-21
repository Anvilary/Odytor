"""Per-profile evidence rules: nothing falls through to pytest's table, and nothing is green without proof."""
import base64
import json

import pytest

from odytor.artifacts import ArtifactLimits, ArtifactStore
from odytor.config import RunSettings
from odytor.jobs import JobService, classify_browser, classify_job, classify_node, classify_playwright_spec

SEL = ('tests',)
HEAD = {'harness': 1, 'kind': 'node_test', 'extract': {'ok': True}, 'argv': ['node', '--test', '--test-reporter=tap', 'tests'],
        'selector_exists': [{'selector': 'tests', 'exists': True, 'is_dir': True}], 'timed_out': False, 'duration_seconds': 0.4,
        'log': {'head': 'TAP version 13', 'tail': '', 'total_bytes': 13, 'truncated': False}, 'tests': [], 'tests_total': 0}


def node(exit=0, counts=None, summary=True, **over):
    counts = {'tests': 3, 'suites': 0, 'pass': 3, 'fail': 0, 'cancelled': 0, 'skipped': 0, 'todo': 0} if counts is None else counts
    report = {**HEAD, 'exit': exit, 'tap': {'summary_found': summary, 'counts': counts, 'malformed_lines': 0, 'truncated': False}, **over}
    return {'schema': 1, 'runner': {'image': 'i'}, 'sandbox': {'timed_out': False, 'container_seconds': 1.0, 'limits': {}}, 'report': report}


def verdict(payload):
    out = classify_node(payload, SEL)
    return out['status'], (out.get('failure') or {}).get('kind')


def test_node_passes_only_on_a_complete_summary_with_executed_passing_tests():
    assert verdict(node()) == ('passed', None)


@pytest.mark.parametrize('payload,expected', [
    (node(counts={'tests': 0, 'suites': 0, 'pass': 0, 'fail': 0, 'cancelled': 0, 'skipped': 0, 'todo': 0}), ('no_tests_collected', 'no_tests_collected')),
    (node(counts={'tests': 2, 'suites': 0, 'pass': 0, 'fail': 0, 'cancelled': 0, 'skipped': 2, 'todo': 0}), ('no_tests_executed', 'no_tests_executed')),
    (node(counts={'tests': 2, 'suites': 0, 'pass': 0, 'fail': 0, 'cancelled': 0, 'skipped': 1, 'todo': 1}), ('no_tests_executed', 'no_tests_executed')),
    (node(exit=1, counts={'tests': 3, 'suites': 0, 'pass': 2, 'fail': 1, 'cancelled': 0, 'skipped': 0, 'todo': 0}), ('failed', 'test_failures')),
    (node(exit=0, counts={'tests': 3, 'suites': 0, 'pass': 2, 'fail': 0, 'cancelled': 1, 'skipped': 0, 'todo': 0}), ('failed', 'test_failures')),
    (node(exit=1), ('failed', 'unexpected_exit')),  # a clean summary but a nonzero exit is never green
    (node(exit=0, summary=False), ('setup_error', 'tap_summary_missing')),
    (node(exit=1, summary=False, counts={}), ('setup_error', 'tap_summary_missing')),
    (node(exit=-9), ('failed', 'killed_by_signal')),
    (node(exit=None), ('setup_error', 'missing_report')),
    (node(counts={'tests': 5, 'suites': 0, 'pass': 2, 'fail': 0, 'cancelled': 0, 'skipped': 1, 'todo': 0}), ('failed', 'incomplete_run')),
    (node(timed_out=True), ('timeout', 'timeout')),
    (node(selector_exists=[{'selector': 'tests', 'exists': False}]), ('no_tests_collected', 'selector_matched_nothing')),
    (node(selector_exists=[]), ('setup_error', 'missing_report')),
    (node(error={'stage': 'extract', 'message': 'bad tar'}), ('setup_error', 'extract')),
    ({'schema': 1, 'setup_error': {'stage': 'profile_unavailable', 'message': 'no node'}}, ('setup_error', 'profile_unavailable')),
    ({'schema': 1, 'sandbox': {'timed_out': True}}, ('timeout', 'timeout')),
    ({'schema': 1, 'sandbox': {'timed_out': False}, 'report': None}, ('setup_error', 'missing_report')),
    ({'schema': 1, 'sandbox': {'timed_out': False}, 'report': {'harness': 1, 'kind': 'test'}}, ('setup_error', 'missing_report')),  # a pytest report is not node evidence
])
def test_node_never_reports_green_without_proof(payload, expected):
    assert verdict(payload) == expected


def test_files_that_define_no_tests_are_never_green_even_though_node_counts_them():
    zero = {'tests': 0, 'suites': 0, 'pass': 0, 'fail': 0, 'cancelled': 0, 'skipped': 0, 'todo': 0}
    payload = node(counts=zero)
    payload['report']['tap']['vacuous_files'] = ['tests/empty.test.mjs']
    out = classify_node(payload, SEL)
    assert (out['status'], out['failure']['kind']) == ('no_tests_collected', 'no_tests_collected')
    assert 'tests/empty.test.mjs' in out['failure']['message'] and out['reports']['vacuous_test_files'] == ['tests/empty.test.mjs']


def test_node_evidence_is_bounded_and_untrusted_types_are_ignored():
    payload = node(tests=[{'id': 'x' * 5000, 'outcome': 'failed' * 10, 'message': 'm' * 5000}, 'junk', None, {'id': 7}], tests_total='many')
    payload['report']['log'] = {'head': 'h' * 99999, 'tail': 't' * 99999, 'total_bytes': 'big', 'truncated': 1}
    out = classify_node(payload, SEL)
    assert len(out['tests'][0]['id']) <= 200 and len(out['log']['head']) <= 4096 and len(out['log']['tail']) <= 8192
    assert out['tests_total'] == 0 and out['log']['total_bytes'] == 0


# --- browser ---------------------------------------------------------------------------------------------------

def steps(*specs, replay=0):
    out = []
    for i, (op, ok) in enumerate(specs):
        item = {'index': i, 'op': op, 'ok': ok, 'ms': 5}
        if ok is None:
            item.update(skipped=True)
        if ok is False:
            item.update(error='AssertionError: expected count eq 3, found 2', assertion=op.startswith('assert_'))
        if i < replay:
            item['replayed'] = True
        out.append(item)
    return out


def browser(step_list, **over):
    report = {'harness': 1, 'kind': 'browser', 'extract': {'ok': True}, 'steps': step_list, 'steps_total': len(step_list),
              'browser': {'name': 'chromium', 'version': '153', 'viewport': {'width': 1280, 'height': 720}, 'locale': 'en-US', 'timezone': 'UTC',
                          'color_scheme': 'light'},
              'console': {'messages': [], 'counts': {}}, 'page_errors': [], 'network': {'total': 0, 'by_path': {}, 'entries': []}, 'snapshots': [],
              'duration_seconds': 1.2, **over}
    return {'schema': 1, 'runner': {}, 'sandbox': {'timed_out': False, 'container_seconds': 2.0, 'limits': {}}, 'report': report}


def bverdict(step_list, replay=0, **over):
    out = classify_browser(browser(step_list, **over), len(step_list), replay)
    return out['status'], (out.get('failure') or {}).get('kind')


@pytest.mark.parametrize('step_list,replay,expected', [
    ([('goto', True), ('click', True), ('assert_text', True)], 0, ('passed', None)),
    ([('goto', True), ('click', True)], 0, ('completed', 'no_assertions')),  # exploration is never a green validation
    ([('goto', True), ('assert_count', False), ('click', None)], 0, ('failed', 'assertion_failed')),
    ([('goto', True), ('click', False)], 0, ('failed', 'step_failed')),
    ([('click', True), ('assert_visible', True)], 0, ('no_tests_executed', 'no_navigation')),
    ([('goto', False), ('assert_visible', None)], 0, ('failed', 'step_failed')),
    ([('goto', True), ('assert_visible', True), ('click', True)], 2, ('completed', 'no_assertions')),  # assertions only in replayed steps do not count
    ([('goto', True), ('assert_visible', True), ('assert_text', True)], 2, ('passed', None)),
    ([('goto', True), ('assert_visible', False), ('click', None)], 2, ('failed', 'replay_diverged')),
])
def test_browser_status_rules(step_list, replay, expected):
    assert bverdict(steps(*step_list, replay=replay), replay=replay) == expected


@pytest.mark.parametrize('payload,expected', [
    ({'schema': 1, 'setup_error': {'stage': 'busy', 'message': 'queue full'}}, ('setup_error', 'busy')),
    ({'schema': 1, 'sandbox': {'timed_out': True}}, ('timeout', 'timeout')),
    ({'schema': 1, 'sandbox': {'timed_out': False, 'output_overflow': True}}, ('setup_error', 'output_overflow')),
    ({'schema': 1, 'sandbox': {'timed_out': False}, 'report': {'harness': 1, 'kind': 'browser', 'error': {'stage': 'startup', 'message': 'no answer'}}},
     ('setup_error', 'startup_failure')),
    ({'schema': 1, 'sandbox': {'timed_out': False}, 'report': None}, ('setup_error', 'missing_report')),
])
def test_browser_environment_and_startup_failures_are_distinct_from_assertion_failures(payload, expected):
    out = classify_browser(payload, 3, 0)
    assert (out['status'], out['failure']['kind']) == expected


def test_browser_report_must_match_the_submitted_scenario_and_deadline_is_a_timeout():
    payload = browser(steps(('goto', True), ('assert_visible', True)))
    assert classify_browser(payload, 5, 0)['failure']['kind'] == 'missing_report'  # forged or truncated step list
    late = browser(steps(('goto', True), ('click', None)), timed_out=True)
    assert classify_browser(late, 2, 0)['status'] == 'timeout'


def test_browser_evidence_from_the_page_is_clipped_and_typed():
    payload = browser(steps(('goto', True), ('assert_visible', True)),
                      console={'messages': [{'type': 'error' * 9, 'text': 'x' * 9000, 'line': 'NaN'}] * 300, 'counts': {'error': 'lots'}},
                      page_errors=['e' * 9000] * 80, network={'total': 1, 'by_path': {f'GET /{i}': 1 for i in range(500)}, 'entries': [{'method': 'GET', 'path': 'p' * 900}] * 900},
                      snapshots=[{'step': 'final', 'aria': 'a' * 99999, 'text': 't' * 99999, 'title': 'T' * 9999}])
    out = classify_browser(payload, 2, 0)
    assert len(out['console']['messages']) == 100 and len(out['console']['messages'][0]['text']) == 300
    assert len(out['page_errors']) == 50 and len(out['page_errors'][0]) == 300
    assert len(out['network']['entries']) == 200 and len(out['network']['entries'][0]['path']) == 200 and len(out['network']['by_path']) == 100
    snap = out['snapshots'][0]
    assert len(snap['aria']) == 12000 and len(snap['text']) == 4000 and len(snap['title']) == 200


def test_dispatch_uses_the_profile_kind_not_the_pytest_table():
    record = {'profile': 'node-test', 'selectors': list(SEL), 'requirements_sha256': ''}
    assert classify_job(record, node())['status'] == 'passed'
    # a payload shaped like pytest evidence must not be accepted for a node run
    assert classify_job(record, {'schema': 1, 'sandbox': {}, 'report': {'harness': 1, 'kind': 'test', 'counts': {'passed': 9}}})['status'] == 'setup_error'
    record = {'profile': 'browser-playwright', 'browser': {'steps_total': 2, 'replay': 0}}
    assert classify_job(record, browser(steps(('goto', True), ('assert_visible', True))))['status'] == 'passed'


# --- artifact intake -------------------------------------------------------------------------------------------

def png():
    import struct, zlib
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')


@pytest.fixture
def intake(tmp_path):
    cfg = RunSettings(runner_command=('true',), policies_path='x', data_dir=str(tmp_path / 'data'))
    store = ArtifactStore(tmp_path / 'art', ArtifactLimits(max_artifact_bytes=1 << 20, max_run_bytes=2 << 20, max_per_run=4))
    service = JobService(cfg, artifacts=store)
    record = {'run_id': 'test-' + 'a' * 32, 'owner': 'alice', 'repo': 'o/r',
              'revisions': {'workspace_id': 'ws-' + 'b' * 32, 'workspace_version': 7, 'source': {'kind': 'pr_head', 'head_sha': 'c' * 40, 'secret': 'x'}},
              'trees': {'effective_tree': 'd' * 40}}
    return service, store, record


def b64(data):
    return base64.b64encode(data).decode()


def test_intake_rederives_everything_and_binds_provenance(intake):
    service, store, record = intake
    payload = {'report': {'artifacts': [{'name': 'after.png', 'kind': 'screenshot', 'b64': b64(png()), 'mime': 'text/html', 'sha256': 'lie', 'bytes': 1}]}}
    [meta] = service._intake_artifacts(record, payload)
    assert meta['mime'] == 'image/png' and meta['size'] == len(png()) and meta['sha256'] != 'lie'  # the runner's claims are ignored
    assert meta['tested_tree'] == 'd' * 40 and meta['workspace_version'] == 7 and meta['workspace_id'] == 'ws-' + 'b' * 32
    assert meta['source_revision'] == {'kind': 'pr_head', 'head_sha': 'c' * 40}  # only known fields are copied
    assert store.content(meta['artifact_id'], 'alice')['image'][1] == png()


def test_intake_reports_bad_artifacts_without_raising_or_storing_them(intake):
    service, store, record = intake
    items = [{'name': 'ok.txt', 'kind': 'log', 'b64': b64(b'fine')},
             {'name': 'fake.png', 'kind': 'screenshot', 'b64': b64(b'<svg onload=x>')},  # not an image
             {'name': 'bad.png', 'kind': 'screenshot', 'b64': '!!!not base64!!!'},
             {'name': 'huge.bin', 'kind': 'log', 'b64': b64(b'x' * ((1 << 20) + 5))}]
    items += [{'name': 'x', 'kind': 'log'}, 'junk', None]  # malformed entries are skipped (and still count against the cap)
    out = service._intake_artifacts(record, {'report': {'artifacts': items}})
    assert sum('artifact_id' in o for o in out) == 1 and len(store.records()) == 1
    errors = [o for o in out if 'error' in o]
    assert {o.get('name') for o in errors} >= {'fake.png', 'bad.png', 'huge.bin'} and len(errors) == 4  # three rejected, one cap note


def test_intake_enforces_count_and_run_budgets(intake):
    service, store, record = intake
    many = [{'name': f'n{i}.txt', 'kind': 'log', 'b64': b64(b'x' * 10)} for i in range(6)]
    out = service._intake_artifacts(record, {'report': {'artifacts': many}})
    assert sum('artifact_id' in o for o in out) == 4 and 'not stored' in out[-1]['error']
    fat = [{'name': f'f{i}.txt', 'kind': 'log', 'b64': b64(b'y' * (900 * 1024))} for i in range(3)]
    out = service._intake_artifacts({**record, 'run_id': 'test-' + 'e' * 32}, {'report': {'artifacts': fat}})
    assert sum('artifact_id' in o for o in out) == 2 and any('budget' in o.get('error', '') for o in out)


def test_intake_without_a_store_discards_and_says_so(tmp_path):
    cfg = RunSettings(runner_command=('true',), policies_path='x', data_dir=str(tmp_path / 'data'))
    out = JobService(cfg)._intake_artifacts({'run_id': 'r', 'owner': 'a', 'repo': 'o/r'}, {'report': {'artifacts': [{'name': 'a', 'kind': 'log', 'b64': 'eA=='}]}})
    assert out == [{'error': 'Artifact storage is not configured; the runner\'s artifacts were discarded.', 'discarded': 1}]


def test_classify_playwright_spec_cases():
    sel = ("tests/e2e/notes-qa/notes-qa-regressions.spec.mjs",)

    def pw_payload(exit_code=0, counts=None, errors=None, tests=None, timed_out=False, report_err=None, log_tail="", server_log=None):
        c = {"collected": 1, "executed": 1, "passed": 1, "failed": 0, "skipped": 0, "flaky": 0, "errors": 0,
             "incomplete": 0, "timed_out": 0}
        if counts:
            c.update(counts)
        pw = {
            "counts": c,
            "specs": ["tests/e2e/notes-qa/notes-qa-regressions.spec.mjs"],
            "tests": tests or [{"id": "t1", "outcome": "passed" if c["passed"] else "skipped"}] * c["collected"],
            "tests_total": c["collected"],
            "selector_coverage": [{"selector": sel[0], "collected": c["collected"], "executed": c["executed"]}],
            "errors": errors or [],
        }
        report = {
            "harness": 1,
            "kind": "playwright_spec",
            "exit": exit_code,
            "timed_out": timed_out,
            "duration_seconds": 1.2,
            "log": {"head": "", "tail": log_tail, "total_bytes": len(log_tail), "truncated": False},
            "server_log": server_log,
            "playwright": pw,
        }
        if report_err:
            report["error"] = report_err
        return {
            "schema": 1,
            "runner": {"image": "i"},
            "sandbox": {"timed_out": timed_out, "container_seconds": 2.0, "limits": {}},
            "report": report,
        }

    # 1. Passed test
    out = classify_playwright_spec(pw_payload(), sel)
    assert out["status"] == "passed"
    assert out["failure"] is None
    assert out["counts"]["passed"] == 1

    # 2. Failed test (assertion failure)
    out = classify_playwright_spec(pw_payload(exit_code=1, counts={"passed": 0, "failed": 1},
                                              tests=[{"id": "t1", "outcome": "failed", "error": "expected 2 to be 3"}]), sel)
    assert out["status"] == "failed"
    assert out["failure"]["kind"] == "assertion_failure"
    assert "expected 2 to be 3" in out["failure"]["message"]

    # 3. Playwright collection error
    out = classify_playwright_spec(pw_payload(exit_code=1, counts={"errors": 1, "passed": 0}, errors=["SyntaxError in spec"]), sel)
    assert out["status"] == "failed"
    assert out["failure"]["kind"] == "collection_failure"

    # 4. App startup failure (scratch server exited during startup)
    out = classify_playwright_spec(pw_payload(exit_code=1, log_tail="RuntimeError: Scratch server exited during startup",
                                              report_err={"stage": "startup", "message": "App server failed to start"}), sel)
    assert out["status"] == "setup_error"
    assert out["failure"]["kind"] == "app_startup_failure"

    # 5. Dependency failure
    out = classify_playwright_spec(pw_payload(exit_code=1, log_tail="ModuleNotFoundError: No module named 'uvicorn'",
                                              report_err={"stage": "dependency", "message": "Missing uvicorn"}), sel)
    assert out["status"] == "setup_error"
    assert out["failure"]["kind"] == "dependency_failure"

    # 6. Zero collected tests
    out = classify_playwright_spec(pw_payload(exit_code=0, counts={"collected": 0, "executed": 0, "passed": 0}), sel)
    assert out["status"] == "no_tests_collected"

    # 7. Skipped-only tests
    out = classify_playwright_spec(pw_payload(exit_code=0, counts={"collected": 1, "executed": 0, "passed": 0, "skipped": 1}), sel)
    assert out["status"] == "no_tests_executed"

    # 8. Timeout
    out = classify_playwright_spec(pw_payload(timed_out=True), sel)
    assert out["status"] == "timeout"

    assert classify_playwright_spec(pw_payload(exit_code=None), sel)["failure"]["kind"] == "missing_report"
    assert classify_playwright_spec(pw_payload(counts={"incomplete": 1}), sel)["failure"]["kind"] == "incomplete_run"
    assert classify_playwright_spec(pw_payload(counts={"timed_out": 1}), sel)["failure"]["kind"] == "test_timeout"
    assert classify_playwright_spec(pw_payload(), sel + ("other/notes-qa-regressions.spec.mjs",))["status"] != "passed"

