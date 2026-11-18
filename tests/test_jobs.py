"""Evidence-based classification and the bounded job service (no real sandbox)."""
import os
import threading
import time

import pytest

from odytor.config import RunSettings
from odytor.errors import ValidationError
from odytor.jobs import JobService, QueueFullError, QueuedJob, RunStore, classify, new_run_id, runner_environment

SPEC = 'a' * 64


def payload(*, exit=0, finished=0, passed=2, failed=0, errors=0, skipped=0, collected=None, coverage=None,
            timed_out=False, sandbox_timeout=False, spec=SPEC, report=True, **report_overrides):
    reported = passed + failed + errors + skipped
    counts = {'collected': reported if collected is None else collected, 'reported': reported, 'passed': passed,
              'failed': failed, 'errors': errors, 'skipped': skipped, 'xfailed': 0, 'xpassed': 0, 'incomplete': 0,
              'executed': passed + failed + errors, 'not_run': 0}
    body = {'harness': 1, 'kind': 'test', 'extract': {'ok': True}, 'argv': ['python', '-m', 'pytest', '--', 'tests'],
            'pytest_exit': exit, 'timed_out': timed_out, 'finished_event': finished, 'duration_seconds': 1.5,
            'counts': counts, 'tests': [{'id': 'tests/a.py::t', 'outcome': 'passed', 'duration': 0.1, 'message': ''}],
            'tests_total': 1, 'selector_coverage': coverage if coverage is not None else
            [{'selector': 'tests', 'collected': counts['collected'], 'executed': counts['executed']}],
            'collect_errors': [], 'malformed_events': 0, 'log': {'head': 'h', 'tail': 't', 'total_bytes': 2, 'truncated': False}}
    body.update(report_overrides)
    return {'schema': 1, 'runner': {'image': 'img', 'image_id': 'f' * 64, 'platform': 'linux/amd64', 'python': '3.12.1'},
            'environment': {'cache_key': 'k' * 40, 'cache_status': 'hit', 'requirements_sha256': spec, 'prepare_seconds': 0.0},
            'sandbox': {'engine_exit_code': 0, 'timed_out': sandbox_timeout, 'container_seconds': 2.0, 'limits': {'network': 'none'}},
            'report': body if report else None}


def status(**kw):
    return classify(payload(**kw), ('tests',), SPEC)


def test_green_requires_executed_passing_tests():
    result = status()
    assert result['status'] == 'passed' and result['failure'] is None
    assert result['environment']['cache_hit'] is True and result['counts']['passed'] == 2


@pytest.mark.parametrize('kwargs,expected,kind', [
    (dict(exit=1, finished=1, passed=1, failed=1), 'failed', 'test_failures'),
    (dict(exit=5, finished=5, passed=0, collected=0), 'no_tests_collected', 'no_tests_collected'),
    (dict(exit=0, passed=0, skipped=3, coverage=[{'selector': 'tests', 'collected': 3, 'executed': 0}]), 'no_tests_executed', 'no_tests_executed'),
    (dict(exit=0, finished=None), 'setup_error', 'missing_report'),
    (dict(report=False), 'setup_error', 'missing_report'),
    (dict(exit=4, finished=None, passed=0), 'setup_error', 'pytest_usage_error'),
    (dict(exit=3, finished=None, passed=0), 'setup_error', 'pytest_internal_error'),
    (dict(exit=2, finished=2, passed=0), 'setup_error', 'interrupted'),
    (dict(exit=-9, finished=None, passed=0), 'failed', 'killed_by_signal'),
    (dict(timed_out=True, exit=-9, finished=None), 'timeout', 'timeout'),
    (dict(sandbox_timeout=True, report=False), 'timeout', 'timeout'),
    (dict(spec='b' * 64), 'setup_error', 'spec_mismatch'),
    (dict(exit=0, coverage=[{'selector': 'tests', 'collected': 2, 'executed': 0}]), 'no_tests_executed', 'selector_not_executed'),
    (dict(exit=0, coverage=[{'selector': 'tests', 'collected': 0, 'executed': 0}]), 'no_tests_collected', 'selector_matched_nothing'),
    (dict(exit=0, coverage=[]), 'setup_error', 'missing_report'),
    (dict(exit=0, error={'stage': 'extract', 'message': 'bad tar'}), 'setup_error', 'extract'),
    (dict(exit=7, finished=7, passed=1), 'failed', 'unexpected_exit'),
])
def test_classification_matrix(kwargs, expected, kind):
    result = status(**kwargs)
    assert (result['status'], result['failure']['kind']) == (expected, kind)
    assert result['status'] != 'passed'


def test_collection_errors_are_setup_errors_with_evidence():
    result = status(exit=2, finished=2, passed=0, collect_errors=[{'id': 'tests/a.py', 'message': 'ImportError'}])
    assert result['failure']['kind'] == 'collection_error' and result['reports']['collect_errors'][0]['message'] == 'ImportError'


def test_runner_setup_error_and_missing_pieces_are_never_green():
    assert classify({'setup_error': {'stage': 'image', 'message': 'gone', 'detail': {'a': 1}}}, ('tests',), SPEC)['status'] == 'setup_error'
    assert classify({}, ('tests',), SPEC)['status'] == 'setup_error'
    assert classify({'schema': 1, 'sandbox': 'x', 'report': []}, ('tests',), SPEC)['status'] == 'setup_error'


def test_hostile_payload_values_are_coerced():
    bad = payload(counts={'passed': 'many', 'collected': -1, 'executed': True}, tests=[{'id': 1, 'outcome': ['x']}, 'str'],
                  log={'head': 5, 'tail': None, 'total_bytes': 'big'}, argv='sh -c x', exit=0)
    result = classify(bad, ('tests',), SPEC)
    assert result['status'] != 'passed'
    assert all(isinstance(v, int) for v in result['counts'].values())
    assert result['execution']['invocation'] == []


def test_log_and_test_truncation_metadata():
    big = payload(tests=[{'id': 'tests/a.py::t%d' % i, 'outcome': 'passed', 'duration': 0.1, 'message': 'x' * 150} for i in range(500)],
                  tests_total=500, log={'head': 'h' * 9000, 'tail': 't' * 20000, 'total_bytes': 99999, 'truncated': True})
    result = classify(big, ('tests',), SPEC)
    assert result['tests_truncated'] is True and len(result['tests']) < 500
    assert len(result['log']['head']) == 4096 and len(result['log']['tail']) == 8192 and result['log']['truncated']


def test_runner_never_inherits_service_credentials(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_secret')
    monkeypatch.setenv('ODYTOR_OAUTH_SIGNING_KEY', 'k' * 40)
    monkeypatch.setenv('ODYTOR_RUNNER_IMAGE', 'sha256:' + 'a' * 64)
    env = runner_environment()
    assert 'GITHUB_TOKEN' not in env and 'ODYTOR_OAUTH_SIGNING_KEY' not in env and env['ODYTOR_RUNNER_IMAGE']


# --- service ---------------------------------------------------------------------------------
def settings(tmp_path, **kw):
    return RunSettings(runner_command=('true',), policies_path='x', data_dir=str(tmp_path / 'data'),
                       min_free_bytes=1, max_source_bytes=1, **kw)


def record(run_id, spool_selectors=('tests',), now=None):
    now = now or time.time()
    return {'run_id': run_id, 'status': 'queued', 'owner': 'u', 'repo': 'o/r', 'pr': 1, 'selectors': list(spool_selectors),
            'requirements_sha256': SPEC, 'timestamps': {'created_at': now, 'started_at': None, 'finished_at': None},
            'expires_at': now + 3600, 'result': None}


def submit(service, tmp_path):
    service.reserve()
    run_id = new_run_id()
    spool = service.spool_dir(run_id)
    (spool / 'source.tar').write_bytes(b'x')
    service.enqueue(record(run_id, now=service.clock()), QueuedJob(run_id, spool, {'job_id': run_id}, 10))
    return run_id, spool


def wait_for(service, run_id, states, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        rec = service.store.load(run_id)
        if rec and rec['status'] in states:
            return rec
        time.sleep(0.02)
    raise AssertionError(f'run stayed {rec and rec["status"]}')


def test_queue_is_bounded_and_spool_is_removed(tmp_path):
    gate = threading.Event()
    service = JobService(settings(tmp_path, max_queued=2), runner=lambda job, cancel: (gate.wait(10), payload())[1])
    try:
        first, spool = submit(service, tmp_path)
        wait_for(service, first, {'running'})
        queued = [submit(service, tmp_path)[0] for _ in range(2)]
        with pytest.raises(QueueFullError):
            service.reserve()
        gate.set()
        for run_id in [first, *queued]:
            assert wait_for(service, run_id, {'passed'})['status'] == 'passed'
        assert not spool.exists() and not list((service.spool_root / service.instance).iterdir())
        service.reserve(); service.release()
    finally:
        gate.set(); service.shutdown()


def test_worker_survives_a_crashing_run(tmp_path):
    calls = []

    def runner(job, cancel):
        calls.append(job.run_id)
        if len(calls) == 1:
            raise RuntimeError('boom')
        return payload()
    service = JobService(settings(tmp_path), runner=runner)
    try:
        first, spool = submit(service, tmp_path)
        assert wait_for(service, first, {'setup_error'})['result']['failure']['kind'] == 'worker_crash'
        second, _ = submit(service, tmp_path)
        assert wait_for(service, second, {'passed'})
        assert not spool.exists()
    finally:
        service.shutdown()


def test_cancel_queued_and_running(tmp_path):
    started = threading.Event()

    def runner(job, cancel):
        started.set()
        cancel.wait(10)
        return {'canceled': True}
    service = JobService(settings(tmp_path), runner=runner)
    try:
        running, spool = submit(service, tmp_path)
        assert started.wait(5)
        queued, queued_spool = submit(service, tmp_path)
        assert service.cancel(queued) == 'canceled' and not queued_spool.exists()
        assert service.store.load(queued)['status'] == 'canceled'
        assert service.cancel(running) == 'canceling'
        assert wait_for(service, running, {'canceled'})['result']['failure']['kind'] == 'canceled'
        assert not spool.exists() and service.cancel(running) == 'not_cancelable'
    finally:
        service.shutdown()


def test_restart_reconciles_unfinished_runs_and_drops_spool(tmp_path):
    cfg = settings(tmp_path)
    store = RunStore(tmp_path / 'data' / 'results')
    store.directory.mkdir(parents=True)
    for state in ('queued', 'running'):
        rec = record(new_run_id()); rec['status'] = state
        store.save(rec)
    (tmp_path / 'data' / 'spool' / 'test-leftover').mkdir(parents=True)
    service = JobService(cfg, runner=lambda j, c: payload())
    try:
        service.start()
        statuses = {r['status']: r['result']['failure']['kind'] for r in store.records()}
        assert statuses == {'setup_error': 'service_restarted'}
        assert [p.name for p in service.spool_root.iterdir()] == []
    finally:
        service.shutdown()


def _running_record(service, gate):
    run_id = new_run_id()
    service.reserve()
    spool = service.spool_dir(run_id)
    (spool / 'source.tar').write_bytes(b'x')
    service.enqueue(record(run_id, now=service.clock()), QueuedJob(run_id, spool, {'job_id': run_id}, 10))
    return run_id, spool


def test_a_second_local_agent_does_not_disturb_the_first_ones_runs(tmp_path):
    """stdio agents share one data directory; a newcomer must only reconcile instances that are really gone."""
    cfg = settings(tmp_path)
    gate = threading.Event()
    first = JobService(cfg, runner=lambda j, c: (gate.wait(20), payload())[1])
    try:
        run_id, spool = _running_record(first, gate)
        wait_for(first, run_id, {'running'})
        second = JobService(cfg, runner=lambda j, c: payload())
        try:
            second.start()
            assert first.store.load(run_id)['status'] == 'running' and spool.exists()
            assert second.get(run_id)['status'] == 'running'  # readable by either process
        finally:
            second.shutdown()
        gate.set()
        assert wait_for(first, run_id, {'passed'})['status'] == 'passed'
    finally:
        gate.set()
        first.shutdown()


def test_runs_of_a_dead_instance_are_closed_and_its_spool_removed(tmp_path):
    cfg = settings(tmp_path)
    gate = threading.Event()
    dead = JobService(cfg, runner=lambda j, c: (gate.wait(20), payload())[1])
    run_id, spool = _running_record(dead, gate)
    wait_for(dead, run_id, {'running'})
    os.close(dead._instance_fd)  # simulate the process dying: the OS drops its liveness lock
    dead._instance_fd = None
    survivor = JobService(cfg, runner=lambda j, c: payload())
    try:
        survivor.start()
        done = survivor.store.load(run_id)
        assert done['status'] == 'setup_error' and done['result']['failure']['kind'] == 'service_restarted'
        assert not spool.exists() and not (tmp_path / 'data' / 'instances' / f'{dead.instance}.lock').exists()
    finally:
        gate.set()
        survivor.shutdown()


def test_retention_purges_only_expired_terminal_results(tmp_path):
    clock = [1000.0]
    service = JobService(settings(tmp_path, retention_hours=1), runner=lambda j, c: payload(), clock=lambda: clock[0])
    try:
        service.start()
        old, fresh, active = (record(new_run_id(), now=clock[0]) for _ in range(3))
        old.update(status='passed', expires_at=1500.0); fresh.update(status='failed', expires_at=9000.0)
        active.update(status='running', expires_at=1.0)
        for rec in (old, fresh, active):
            service.store.save(rec)
        clock[0] = 2000.0
        assert service.purge(force=True) == 1
        assert service.store.load(old['run_id']) is None and service.store.load(fresh['run_id']) and service.store.load(active['run_id'])
    finally:
        service.shutdown()


def test_run_ids_are_validated_before_touching_the_filesystem(tmp_path):
    store = RunStore(tmp_path)
    for bad in ('../x', 'test-../../etc', 'test-' + 'g' * 32, '', None, 5):
        with pytest.raises(ValidationError):
            store.path(bad)


@pytest.mark.parametrize('script,stage', [
    ('kill -9 $$', 'runner'), ('echo not-json', 'protocol'), ('echo "[1]"', 'protocol'), ('echo boom >&2; exit 3', 'runner'),
    ('head -c 5000000 /dev/zero', 'runner'), ('sleep 30', 'runner')])
def test_runner_transport_failures_are_setup_errors(tmp_path, script, stage):
    from odytor.jobs import make_subprocess_runner
    spool = tmp_path / 'spool'
    spool.mkdir()
    (spool / 'source.tar').write_bytes(b'x' * 100)
    job = QueuedJob(new_run_id(), spool, {'schema': 1}, 1)
    started = time.time()
    result = make_subprocess_runner(('sh', '-c', script), extra_wait=0)(job, threading.Event())
    assert result['setup_error']['stage'] == stage and time.time() - started < 15
    assert classify(result, ('tests',), SPEC)['status'] == 'setup_error'


def test_transport_cancellation_kills_the_runner(tmp_path):
    from odytor.jobs import make_subprocess_runner
    spool = tmp_path / 'spool'
    spool.mkdir()
    (spool / 'source.tar').write_bytes(b'x')
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    result = make_subprocess_runner(('sleep', '60'))(QueuedJob(new_run_id(), spool, {}, 60), cancel)
    assert result == {'canceled': True} and time.time() - started < 10
