"""Real rootless-container integration tests for the execution boundary.

These run the actual runner (`odytor.sandbox`) through Odytor's real subprocess transport and
classifier. They need podman and a pinned local image; set ODYTOR_TEST_RUNNER_IMAGE to its ID or digest
(for example a local python:3.12 image). They are skipped, loudly, otherwise.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
from unittest import mock

import pytest

from odytor.jobs import QueuedJob, classify, make_subprocess_runner, new_run_id
from odytor.profile import requirements_digest, validate_requirements

IMAGE = os.environ.get('ODYTOR_TEST_RUNNER_IMAGE')
pytestmark = pytest.mark.skipif(not (shutil.which('podman') and IMAGE),
                                reason='needs podman and ODYTOR_TEST_RUNNER_IMAGE (a pinned local image)')
PYTEST_PIN = os.environ.get('ODYTOR_TEST_PYTEST_PIN', 'pytest==8.3.5')
REQ = validate_requirements(PYTEST_PIN + '\n')
SPEC = requirements_digest(REQ)


@pytest.fixture(scope='module')
def root(tmp_path_factory):
    return tmp_path_factory.mktemp('runner')


def make_tar(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, text in files.items():
            data = textwrap.dedent(text).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def execute(root, tmp_path, files=None, *, selectors=('tests',), timeout=60, requirements=REQ, raw_tar=None, env=None, overlay_paths=()):
    run_id = new_run_id()
    spool = tmp_path / run_id
    spool.mkdir()
    (spool / 'source.tar').write_bytes(raw_tar if raw_tar is not None else make_tar(files))
    header = {'schema': 1, 'job_id': run_id, 'repo': 'owner/repo', 'profile': 'python-pytest', 'profile_version': '1',
              'test_dirs': ['tests'], 'selectors': list(selectors), 'requirements': requirements, 'timeout_seconds': timeout,
              'overlay_paths': list(overlay_paths)}
    overrides = {'ODYTOR_RUNNER_IMAGE': IMAGE, 'ODYTOR_RUNNER_ROOT': str(root), 'ODYTOR_RUNNER_MAX_TIMEOUT': '600',
                 **(env or {})}
    with mock.patch.dict(os.environ, overrides):
        payload = make_subprocess_runner((sys.executable, '-m', 'odytor.sandbox'))(QueuedJob(run_id, spool, header, timeout), threading.Event())
    return payload, classify(payload, selectors, requirements_digest(requirements), tuple(overlay_paths))


def containers():
    out = subprocess.run(['podman', 'ps', '-a', '--format', '{{.Names}}'], capture_output=True, text=True).stdout.split()
    return [name for name in out if name.startswith('gp-')]


def test_passed_failed_skipped_and_per_test_evidence(root, tmp_path):
    payload, out = execute(root, tmp_path, {'tests/test_a.py': '''
        import pytest
        def test_ok(): pass
        class TestK:
            def test_m(self): pass
        @pytest.mark.parametrize('v', [1, 2])
        def test_p(v): pass
    '''})
    assert out['status'] == 'passed', out
    assert out['counts']['passed'] == 4 and out['counts']['collected'] == 4 and out['counts']['executed'] == 4
    assert {t['id'] for t in out['tests']} >= {'tests/test_a.py::test_ok', 'tests/test_a.py::TestK::test_m', 'tests/test_a.py::test_p[1]'}
    assert out['execution']['invocation'][-2:] == ['--', 'tests'] and out['execution']['invocation'][:3] == ['python', '-m', 'pytest']
    assert out['runner']['image_id'] and out['runner']['python'].startswith('3.')
    assert out['selector_coverage'] == [{'selector': 'tests', 'collected': 4, 'executed': 4}]

    _, failed = execute(root, tmp_path, {'tests/test_a.py': 'def test_ok(): pass\ndef test_bad(): assert 1 == 2\n'})
    assert failed['status'] == 'failed' and failed['counts']['failed'] == 1 and failed['counts']['passed'] == 1
    assert any(t['outcome'] == 'failed' and 'assert 1 == 2' in t['message'] for t in failed['tests'])

    _, skipped = execute(root, tmp_path, {'tests/test_a.py': 'import pytest\n@pytest.mark.skip\ndef test_s(): pass\n'})
    assert skipped['status'] == 'no_tests_executed' and skipped['counts']['skipped'] == 1


def test_xfail_xpass_and_setup_errors_are_distinguished(root, tmp_path):
    _, out = execute(root, tmp_path, {'tests/test_a.py': '''
        import pytest
        @pytest.mark.xfail
        def test_x(): assert False
        @pytest.mark.xfail
        def test_xp(): pass
        @pytest.fixture
        def broken(): raise RuntimeError('fixture')
        def test_e(broken): pass
        def test_p(): pass
    '''})
    by = {t['id'].split('::')[-1]: t['outcome'] for t in out['tests']}
    assert by == {'test_e': 'error', 'test_xp': 'xpassed', 'test_x': 'xfailed', 'test_p': 'passed'}
    assert out['status'] == 'failed'


def test_zero_tests_collected_and_collection_errors_are_never_green(root, tmp_path):
    _, none = execute(root, tmp_path, {'tests/helper.py': 'X = 1\n'})
    assert none['status'] == 'no_tests_collected'
    _, broken = execute(root, tmp_path, {'tests/test_a.py': 'import module_that_does_not_exist\ndef test_a(): pass\n'})
    assert broken['status'] == 'setup_error' and broken['failure']['kind'] == 'collection_error'
    _, missing = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'}, selectors=('tests/test_a.py::test_nope',))
    assert missing['status'] == 'setup_error' and missing['failure']['kind'] == 'pytest_usage_error'


def test_missing_report_even_when_process_exits_zero(root, tmp_path):
    _, out = execute(root, tmp_path, {'tests/conftest.py': 'import os\nos._exit(0)\n', 'tests/test_a.py': 'def test_a(): pass\n'})
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'missing_report'


def test_corrupt_artifact_is_a_setup_error(root, tmp_path):
    _, out = execute(root, tmp_path, raw_tar=b'definitely not a tar archive')
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'extract'


def test_timeout_reports_partial_evidence_and_removes_the_container(root, tmp_path):
    started = time.time()
    _, out = execute(root, tmp_path, {'tests/test_a.py': 'import time\ndef test_fast(): pass\ndef test_slow(): time.sleep(120)\n'}, timeout=10)
    assert out['status'] == 'timeout' and time.time() - started < 60
    assert out['counts']['passed'] == 1
    assert containers() == []


def test_log_truncation_is_explicit(root, tmp_path):
    _, out = execute(root, tmp_path, {'tests/test_a.py': 'def test_a():\n    print("x" * 200000)\n    assert False\n'})
    assert out['status'] == 'failed' and out['log']['truncated'] is True and out['log']['total_bytes'] > 200000
    assert len(out['log']['head']) <= 4096 and len(out['log']['tail']) <= 8192


def test_selector_metacharacters_are_arguments_never_shell(root, tmp_path):
    files = {'tests/test_a.py': '''
        import os, pytest
        @pytest.mark.parametrize('v', ['x;touch /tmp/pwned', '$(touch /tmp/pwned)'])
        def test_p(v): assert not os.path.exists('/tmp/pwned')
    '''}
    _, out = execute(root, tmp_path, files, selectors=('tests/test_a.py::test_p[x;touch /tmp/pwned]',))
    assert out['status'] == 'passed' and out['counts']['executed'] == 1


def test_dependency_failure_is_a_setup_error_and_leaves_no_environment(root, tmp_path):
    bad = validate_requirements(PYTEST_PIN + '\ngit-plus-no-such-package-xyz==9.9.9\n')
    _, out = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'}, requirements=bad)
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'prepare'
    envs = root / 'envs'
    key = out['environment']['cache_key']  # not produced on failure: nothing must claim to be ready
    assert key == '' and not [p for p in envs.iterdir() if p.is_dir() and (p / '.gp-ready').exists() and bad in str(p)]
    assert not [p for p in envs.iterdir() if p.name.startswith('.prep-')]


def test_sandbox_boundary_from_inside(root, tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_SENTINEL_TOKEN')
    monkeypatch.setenv('ODYTOR_OAUTH_SIGNING_KEY', 'SENTINEL_SIGNING_KEY_' + 'x' * 20)
    monkeypatch.setenv('ODYTOR_ODYSSEUS_PAT', 'SENTINEL_PAT')
    _, out = execute(root, tmp_path, {'tests/test_boundary.py': f'''
        import os, socket, subprocess, pathlib, pytest
        HOME_USER = {os.path.basename(os.path.expanduser('~'))!r}

        def test_no_credentials_in_environment_or_proc():
            blob = repr(dict(os.environ)) + pathlib.Path('/proc/self/environ').read_text(errors='replace')
            assert 'SENTINEL' not in blob and 'GITHUB' not in blob and 'GIT_PLUS' not in blob.replace('ODYTOR_EVENTS', '')

        def test_unprivileged_and_no_capabilities():
            assert os.getuid() == 65534
            status = pathlib.Path('/proc/self/status').read_text()
            assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status

        def test_no_network():
            with pytest.raises(OSError):
                socket.create_connection(('1.1.1.1', 53), timeout=3)
            with pytest.raises(OSError):
                socket.getaddrinfo('github.com', 443)
            assert sorted(os.listdir('/sys/class/net')) == ['lo']

        def test_no_host_mounts_or_sockets():
            for path in ('/var/run/docker.sock', '/run/docker.sock', '/run/podman', '/run/user', '/var/run/secrets'):
                assert not os.path.exists(path), path
            assert HOME_USER not in os.listdir('/home') if os.path.isdir('/home') else True
            mounts = pathlib.Path('/proc/self/mountinfo').read_text()
            assert 'odytor-runner' not in mounts and 'git-plus-runner' not in mounts and '.local/state' not in mounts and '/.git' not in mounts

        def test_filesystem_is_read_only_except_tmpfs():
            for path in ('/etc/pwned', '/usr/pwned', '/opt/env/pwned', '/in/pwned', '/opt/env/site/pwned'):
                with pytest.raises(OSError):
                    open(path, 'w')
            open('/tmp/ok', 'w').close(); open('/work/ok', 'w').close()

        def test_pid_namespace_is_private():
            assert len([p for p in os.listdir('/proc') if p.isdigit()]) < 12

        def test_kernel_limits_are_applied():
            cg = pathlib.Path('/sys/fs/cgroup')
            assert cg.joinpath('memory.max').read_text().strip() == str(4096 * 1024 * 1024)
            assert cg.joinpath('cpu.max').read_text().split()[0] == '200000'
            assert cg.joinpath('pids.max').read_text().strip() == '512'
    '''})
    assert out['status'] == 'passed', (out['failure'], out['tests'], out['log'])
    assert out['counts']['passed'] == 7
    assert out['execution']['limits']['network'] == 'none'


def test_resource_limits_are_enforced(root, tmp_path):
    env = {'ODYTOR_RUNNER_MEMORY_MIB': '256', 'ODYTOR_RUNNER_PIDS': '64', 'ODYTOR_RUNNER_TMP_MIB': '16'}
    _, disk = execute(root, tmp_path, {'tests/test_d.py': '''
        import pytest
        def test_disk():
            with pytest.raises(OSError):
                with open('/tmp/big', 'wb') as f:
                    for _ in range(64): f.write(b'0' * 1024 * 1024)
    '''}, env=env)
    assert disk['status'] == 'passed', disk['log']
    _, pids = execute(root, tmp_path, {'tests/test_p.py': '''
        import subprocess, pytest
        def test_pids():
            procs = []
            try:
                with pytest.raises((OSError, BlockingIOError)):
                    for _ in range(500): procs.append(subprocess.Popen(['sleep', '30']))
            finally:
                for p in procs: p.kill()
    '''}, env=env)
    assert pids['status'] == 'passed', pids['log']
    _, mem = execute(root, tmp_path, {'tests/test_m.py': 'def test_mem():\n    blocks = [bytearray(64 * 1024 * 1024) for _ in range(40)]\n'}, env=env)
    assert mem['status'] == 'failed' and mem['failure']['kind'] in {'killed_by_signal', 'test_failures'}


def test_jobs_cannot_contaminate_each_other_or_the_environment(root, tmp_path):
    def snapshot():
        digest = hashlib.sha256()
        for env in sorted((root / 'envs').glob('*/site')):
            for path in sorted(env.rglob('*')):
                if path.is_file():
                    digest.update(f'{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}'.encode())
        return digest.hexdigest()
    execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'})
    before = snapshot()
    _, first = execute(root, tmp_path, {'tests/test_w.py': '''
        import pathlib, sys, pytest
        def test_write_everywhere_allowed():
            pathlib.Path('/tmp/leak').write_text('x'); pathlib.Path('/work/leak').write_text('x')
            pathlib.Path('/tmp/home/leak').write_text('x')
        def test_cannot_poison_dependencies():
            site = next(p for p in sys.path if p.endswith('/opt/env/site'))
            with pytest.raises(OSError):
                open(site + '/pytest_report.py', 'w')
            with pytest.raises(OSError):
                open(site + '/sitecustomize.py', 'w')
    '''})
    assert first['status'] == 'passed'
    _, second = execute(root, tmp_path, {'tests/test_r.py': '''
        import os
        def test_clean():
            assert not any(os.path.exists(p) for p in ('/tmp/leak', '/work/leak', '/tmp/home/leak', '/work/tests/test_w.py'))
    '''})
    assert second['status'] == 'passed'
    assert snapshot() == before
    assert second['environment']['cache_status'] == 'hit'
    assert not list((root / 'jobs').iterdir()) and containers() == []


def test_sigterm_removes_the_container_and_workspace(root, tmp_path):
    from odytor.jobs import _frame
    run_id = new_run_id()
    (tmp_path / 'source.tar').write_bytes(make_tar({'tests/test_a.py': 'import time\ndef test_a(): time.sleep(100)\n'}))
    header = {'schema': 1, 'job_id': run_id, 'repo': 'owner/repo', 'profile': 'python-pytest', 'profile_version': '1',
              'test_dirs': ['tests'], 'selectors': ['tests'], 'requirements': REQ, 'timeout_seconds': 100}
    env = {**os.environ, 'ODYTOR_RUNNER_IMAGE': IMAGE, 'ODYTOR_RUNNER_ROOT': str(root)}
    proc = subprocess.Popen([sys.executable, '-m', 'odytor.sandbox'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    for chunk in _frame(header, tmp_path / 'source.tar'):
        proc.stdin.write(chunk)
    proc.stdin.flush()  # stdin stays open: EOF now means the client is gone
    deadline = time.time() + 60
    while time.time() < deadline and not any(n.startswith('gp-run-') for n in containers()):
        time.sleep(0.2)
    assert any(n.startswith('gp-run-') for n in containers())
    proc.terminate()
    assert proc.wait(30) == 143
    assert containers() == [] and not list((root / 'jobs').iterdir())


def _start_sleeper(root, tmp_path, run_id=None):
    from odytor.jobs import _frame
    run_id = run_id or new_run_id()
    (tmp_path / 'source.tar').write_bytes(make_tar({'tests/test_a.py': 'import time\ndef test_a(): time.sleep(100)\n'}))
    header = {'schema': 1, 'job_id': run_id, 'repo': 'owner/repo', 'profile': 'python-pytest', 'profile_version': '1',
              'test_dirs': ['tests'], 'selectors': ['tests'], 'requirements': REQ, 'timeout_seconds': 100}
    env = {**os.environ, 'ODYTOR_RUNNER_IMAGE': IMAGE, 'ODYTOR_RUNNER_ROOT': str(root)}
    proc = subprocess.Popen([sys.executable, '-m', 'odytor.sandbox'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    for chunk in _frame(header, tmp_path / 'source.tar'):
        proc.stdin.write(chunk)
    proc.stdin.flush()
    deadline = time.time() + 60
    while time.time() < deadline and not any(n.startswith('gp-run-') for n in containers()):
        time.sleep(0.2)
    assert any(n.startswith('gp-run-') for n in containers())
    return proc, env


def test_client_disconnect_removes_the_container_and_workspace(root, tmp_path):
    proc, _ = _start_sleeper(root, tmp_path)
    proc.stdin.close()  # EOF on stdin is how a lost SSH session looks to the runner
    assert proc.wait(30) == 143
    assert containers() == [] and not list((root / 'jobs').iterdir())


def test_sigkilled_runner_orphan_is_reaped_by_housekeeping_and_never_by_a_live_job(root, tmp_path):
    proc, env = _start_sleeper(root, tmp_path)
    proc.kill()  # nothing can clean up after SIGKILL
    proc.wait()
    assert any(n.startswith('gp-run-') for n in containers())  # the documented residual: it lives until its hard timeout
    done = subprocess.run([sys.executable, '-m', 'odytor.sandbox', 'gc'], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)['orphan_containers_removed']
    assert containers() == []
    subprocess.run(['rm', '-rf', *map(str, (root / 'jobs').glob('job-*'))])


def test_gc_leaves_a_live_jobs_container_alone(root, tmp_path):
    proc, env = _start_sleeper(root, tmp_path)
    try:
        done = subprocess.run([sys.executable, '-m', 'odytor.sandbox', 'gc'], env=env, capture_output=True, text=True, timeout=120)
        assert json.loads(done.stdout)['orphan_containers_removed'] == []
        assert any(n.startswith('gp-run-') for n in containers())
    finally:
        proc.terminate()
        proc.wait(30)
    assert containers() == []


def test_duplicate_job_id_is_rejected_without_touching_the_first(root, tmp_path):
    run_id = new_run_id()
    first, env = _start_sleeper(root, tmp_path, run_id)
    try:
        from odytor.jobs import _frame
        (tmp_path / 'dup').mkdir()
        (tmp_path / 'dup' / 'source.tar').write_bytes(make_tar({'tests/test_a.py': 'def test_a(): pass\n'}))
        header = {'schema': 1, 'job_id': run_id, 'repo': 'owner/repo', 'profile': 'python-pytest', 'profile_version': '1',
                  'test_dirs': ['tests'], 'selectors': ['tests'], 'requirements': REQ, 'timeout_seconds': 60}
        second = subprocess.Popen([sys.executable, '-m', 'odytor.sandbox'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        for chunk in _frame(header, tmp_path / 'dup' / 'source.tar'):
            second.stdin.write(chunk)
        second.stdin.flush()
        out = json.loads(second.stdout.read())
        second.stdin.close()
        assert out['setup_error']['stage'] == 'duplicate'
        assert any(n.startswith('gp-run-') for n in containers())
    finally:
        first.terminate()
        first.wait(30)
    assert containers() == []


def test_stalled_client_is_cut_off_by_the_frame_deadline(root, tmp_path):
    env = {**os.environ, 'ODYTOR_RUNNER_IMAGE': IMAGE, 'ODYTOR_RUNNER_ROOT': str(root), 'ODYTOR_RUNNER_FRAME_TIMEOUT': '10'}
    proc = subprocess.Popen([sys.executable, '-m', 'odytor.sandbox'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    proc.stdin.write((100).to_bytes(4, 'big') + b'{')  # a header that never completes
    proc.stdin.flush()
    started = time.time()
    out = json.loads(proc.stdout.read())
    proc.stdin.close()
    assert out['setup_error']['stage'] == 'frame' and 'Timed out' in out['setup_error']['message']
    assert time.time() - started < 30 and not list((root / 'jobs').iterdir())


# --- phase 2: environment reuse and lifecycle through the real runner ------------------------------
def env_dirs(root):
    return sorted(p.name for p in (root / 'envs').iterdir() if p.is_dir() and (p / '.gp-ready').exists())


def test_environment_is_prepared_once_then_reused_across_different_sources(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('reuse')
    _, first = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'})
    _, second = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): assert True\n', 'src/changed.py': 'X = 2\n'})
    assert (first['environment']['cache_status'], second['environment']['cache_status']) == ('miss', 'hit')
    assert first['environment']['prepare_seconds'] > 0 and second['environment']['prepare_seconds'] == 0
    assert first['environment']['cache_key'] == second['environment']['cache_key'] and len(env_dirs(root)) == 1
    assert second['storage']['cache_totals'] == {'hits': 1, 'misses': 1}
    assert second['storage']['job_workspace_removed'] is True and second['storage']['env_count_before'] == 1


def test_changed_dependencies_get_a_new_environment_and_budget_evicts_the_old_one(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('budget')
    files = {'tests/test_a.py': 'def test_a(): pass\n'}
    _, first = execute(root, tmp_path, files)
    changed = validate_requirements(PYTEST_PIN + '\nattrs==24.2.0\n')
    _, second = execute(root, tmp_path, files, requirements=changed)
    assert second['environment']['cache_status'] == 'miss' and second['environment']['cache_key'] != first['environment']['cache_key']
    assert len(env_dirs(root)) == 2
    _, third = execute(root, tmp_path, files, requirements=changed, env={'ODYTOR_RUNNER_ENV_BUDGET_MIB': '1'})
    evicted = {e['key']: e['reason'] for e in third['storage']['evicted']}
    assert evicted == {first['environment']['cache_key']: 'disk_budget'} and third['environment']['cache_status'] == 'hit'
    assert env_dirs(root) == [second['environment']['cache_key']]
    assert third['storage']['env_bytes_after'] < third['storage']['env_bytes_before']


def test_insufficient_disk_rejects_the_job_with_a_clear_diagnostic(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('full')
    payload, out = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'}, env={'ODYTOR_RUNNER_MIN_FREE_MIB': '4000000'})
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'resources'
    assert 'Insufficient free disk space' in out['failure']['message'] and not env_dirs(root) and not list((root / 'jobs').iterdir())


def test_runner_recovers_from_crash_debris_before_running(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('debris')
    old = time.time() - 7200
    for path in (root / 'jobs' / 'job-dead', root / 'envs' / '.prep-abandoned', root / 'envs' / '.evict-half'):
        path.mkdir(parents=True)
        (path / 'junk').write_text('x')
        os.utime(path, (old, old))
    payload, out = execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'})
    assert out['status'] == 'passed' and out['storage']['debris_removed'] == 3
    assert not (root / 'jobs' / 'job-dead').exists() and not (root / 'envs' / '.prep-abandoned').exists()


def test_concurrent_runners_share_one_environment_preparation(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('concurrent')
    results = {}

    def go(name):
        sub = tmp_path / name
        sub.mkdir()
        results[name] = execute(root, sub, {'tests/test_a.py': 'import time\ndef test_a(): time.sleep(0.5)\n'})[1]
    threads = [threading.Thread(target=go, args=(n,)) for n in ('a', 'b', 'c')]
    [t.start() for t in threads]; [t.join(300) for t in threads]
    statuses = sorted(r['environment']['cache_status'] for r in results.values())
    assert all(r['status'] == 'passed' for r in results.values()) and statuses.count('miss') == 1
    assert len(env_dirs(root)) == 1 and results['a']['environment']['cache_key'] == results['b']['environment']['cache_key']


def test_overlay_test_executes_against_real_production_code_path(root, tmp_path):
    files = {'app/guard.py': 'def is_admin(user): return user == "admin"\n',
             'tests/test_existing.py': 'def test_old(): pass\n',
             'tests/test_overlay.py': 'from app.guard import is_admin\ndef test_reviewer_regression():\n    assert not is_admin("owner")\n'}
    _, out = execute(root, tmp_path, files, overlay_paths=('tests/test_overlay.py',))
    assert out['status'] == 'passed' and out['overlay_coverage'] == [{'path': 'tests/test_overlay.py', 'collected': 1, 'executed': 1}]
    _, unselected = execute(root, tmp_path, files, selectors=('tests/test_existing.py',), overlay_paths=('tests/test_overlay.py',))
    assert unselected['status'] == 'no_tests_executed' and unselected['failure']['kind'] == 'overlay_not_executed'
    files['tests/test_overlay.py'] = files['tests/test_overlay.py'].replace('not is_admin("owner")', 'is_admin("owner")')
    _, failing = execute(root, tmp_path, files, overlay_paths=('tests/test_overlay.py',))
    assert failing['status'] == 'failed' and any(t['id'].endswith('test_reviewer_regression') and t['outcome'] == 'failed' for t in failing['tests'])


# --- host-wide admission with real containers -------------------------------------------------------------------

SLOW = {'tests/test_a.py': 'import time\ndef test_a(): time.sleep(5)\n'}


def test_concurrent_jobs_from_independent_clients_run_one_at_a_time(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('admission')
    execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'})  # prepare the environment once
    results, peak, stop = {}, [0], threading.Event()

    def watch():
        while not stop.is_set():
            peak[0] = max(peak[0], len([n for n in containers() if n.startswith('gp-run-')]))
            time.sleep(0.2)
    threading.Thread(target=watch, daemon=True).start()

    def go(name):
        sub = tmp_path / name
        sub.mkdir()
        results[name] = execute(root, sub, SLOW)[1]
    threads = []
    for name in ('first', 'second', 'third'):
        threads.append(threading.Thread(target=go, args=(name,)))
        threads[-1].start()
        time.sleep(0.5)
    for thread in threads:
        thread.join(300)
    stop.set()
    assert {r['status'] for r in results.values()} == {'passed'}
    assert peak[0] == 1  # never two containers at once, however many clients
    waits = sorted(r['execution']['runner_queue_wait_seconds'] for r in results.values())
    assert waits[0] < 2 and waits[1] > 3 and waits[2] > 8  # served one after another
    assert containers() == [] and not list((root / 'queue').glob('*.ticket')) and not list((root / 'jobs').iterdir())


def test_full_queue_is_refused_not_run_and_never_green(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp('admission-full')
    execute(root, tmp_path, {'tests/test_a.py': 'def test_a(): pass\n'})
    env = {'ODYTOR_RUNNER_MAX_QUEUED': '0'}
    sub = tmp_path / 'holder'
    sub.mkdir()
    holder = threading.Thread(target=lambda: execute(root, sub, SLOW, env=env))
    holder.start()
    deadline = time.time() + 60
    while time.time() < deadline and not any(n.startswith('gp-run-') for n in containers()):
        time.sleep(0.2)
    other = tmp_path / 'other'
    other.mkdir()
    _, refused = execute(root, other, {'tests/test_a.py': 'def test_a(): pass\n'}, env=env)
    holder.join(120)
    assert refused['status'] == 'setup_error' and refused['failure']['kind'] == 'busy'
