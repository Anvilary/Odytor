"""Real SSH transport tests against a provisioned runner host (a VM built by deploy/runner/provision.sh).

Nothing here is mocked: a real OpenSSH client talks to a real sshd whose account is restricted to the forced
command, which starts a real rootless Podman container. They are skipped unless GIT_PLUS_TEST_SSH_RUNNER_DIR
names a directory holding `runner` (the Git Plus identity), `admin` (a key for a sudo-capable maintenance
account named vmadmin) and `known_hosts` (the pinned host key). Optional: GIT_PLUS_TEST_SSH_RUNNER_PORT
(default 2222) and GIT_PLUS_TEST_SSH_RUNNER_HOST (default 127.0.0.1).
"""
import io
import json
import os
import signal
import socket
import subprocess
import tarfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from git_plus.config import RunSettings
from git_plus.jobs import JobService, QueuedJob, classify, make_subprocess_runner, new_run_id
from git_plus.profile import requirements_digest, validate_requirements

KEYS = os.environ.get('GIT_PLUS_TEST_SSH_RUNNER_DIR')
HOST = os.environ.get('GIT_PLUS_TEST_SSH_RUNNER_HOST', '127.0.0.1')
PORT = os.environ.get('GIT_PLUS_TEST_SSH_RUNNER_PORT', '2222')
pytestmark = pytest.mark.skipif(not KEYS, reason='needs GIT_PLUS_TEST_SSH_RUNNER_DIR (a provisioned runner VM)')
REQ = validate_requirements('pytest==8.3.5\n')
SPEC = requirements_digest(REQ)
SSH_OPTS = ['-T', '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ClearAllForwardings=yes',
            '-o', 'ForwardAgent=no', '-o', 'LogLevel=ERROR']


def ssh_command(*, key='runner', user='gp-runner', known_hosts='known_hosts', host=HOST, port=PORT, extra=()):
    return ['ssh', *SSH_OPTS, *extra, '-i', f'{KEYS}/{key}', '-o', f'UserKnownHostsFile={known_hosts if "/" in known_hosts else f"{KEYS}/{known_hosts}"}',
            '-p', str(port), f'{user}@{host}']


RUNNER = ssh_command() if KEYS else []


def vm(script, timeout=60):
    """Run a shell snippet on the VM as the maintenance account."""
    done = subprocess.run(ssh_command(key='admin', user='vmadmin') + [script], capture_output=True, text=True, timeout=timeout)
    return done.stdout


def as_runner(script):
    return vm('cd /; u=$(id -u gp-runner); sudo -u gp-runner env XDG_RUNTIME_DIR=/run/user/$u '
              'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus HOME=/home/gp-runner sh -c ' + json.dumps(script))


def containers():
    return [n for n in as_runner("podman ps -a --format '{{.Names}}'").split() if n.startswith('gp-')]


def workspaces():
    return [n for n in as_runner('ls /home/gp-runner/.local/state/git-plus-runner/jobs 2>/dev/null').split() if n.startswith('job-')]


def wait_until(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, text in files.items():
            data = textwrap.dedent(text).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def header_for(run_id, *, selectors=('tests',), timeout=60, requirements=REQ):
    return {'schema': 1, 'job_id': run_id, 'repo': 'owner/repo', 'profile': 'python-pytest', 'profile_version': '1',
            'test_dirs': ['tests'], 'selectors': list(selectors), 'requirements': requirements, 'timeout_seconds': timeout,
            'overlay_paths': []}


def raw_frame(header, artifact=b'', *, claimed_length=None):
    encoded = json.dumps(header, separators=(',', ':')).encode()
    return len(encoded).to_bytes(4, 'big') + encoded + (len(artifact) if claimed_length is None else claimed_length).to_bytes(8, 'big') + artifact


def submit(tmp_path, files, *, run_id=None, timeout=60, selectors=('tests',), command=None, requirements=REQ):
    """Submit through Git Plus's real transport and classifier."""
    run_id = run_id or new_run_id()
    spool = tmp_path / run_id
    spool.mkdir()
    (spool / 'source.tar').write_bytes(tar_bytes(files))
    payload = make_subprocess_runner(tuple(command or RUNNER))(QueuedJob(run_id, spool, header_for(run_id, selectors=selectors, timeout=timeout, requirements=requirements), timeout), threading.Event())
    return payload, classify(payload, tuple(selectors), requirements_digest(requirements), ())


def exchange(frame, command=None, *, timeout=60, hold=True):
    """Send raw bytes keeping stdin open until the answer arrives, exactly as the real client does."""
    proc = subprocess.Popen(command or RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(frame)
        proc.stdin.flush()
        if not hold:
            proc.stdin.close()
        out = proc.stdout.read()
        proc.wait(timeout)
        return proc.returncode, out, proc.stderr.read().decode(errors='replace')
    finally:
        proc.kill()


@pytest.fixture(autouse=True)
def clean_vm_afterwards():
    yield
    assert wait_until(lambda: not containers() and not workspaces(), 40), (containers(), workspaces())


SLEEPER = {'tests/test_a.py': 'import time\ndef test_a(): time.sleep(100)\n'}
OK = {'tests/test_a.py': 'def test_a(): pass\n'}


# --- authorized path --------------------------------------------------------------------------------

def test_authorized_job_runs_in_the_isolated_runner_and_cleans_up(tmp_path):
    payload, out = submit(tmp_path, {'tests/test_a.py': 'def test_a(): pass\ndef test_b(): assert 2 + 2 == 4\n'})
    assert out['status'] == 'passed' and out['counts']['passed'] == 2, out
    assert payload['cleanup']['job_workspace_removed'] is True
    assert payload['runner']['engine'] == 'podman' and payload['sandbox']['limits']['network'] == 'none'
    assert payload['runner']['image_digest'] and len(payload['runner']['image_id']) == 64


def test_repeat_jobs_reuse_the_dependency_environment(tmp_path):
    first = submit(tmp_path, OK)[1]
    second = submit(tmp_path, {'tests/test_a.py': 'def test_a(): assert True\n'})[1]
    assert first['environment']['cache_key'] == second['environment']['cache_key']
    assert second['environment']['cache_status'] == 'hit' and second['environment']['prepare_seconds'] == 0


def test_adversarial_fixture_inside_the_remote_boundary(tmp_path):
    files = {'tests/test_a.py': '''
        import os, socket, pathlib, pytest
        def test_no_credentials():
            joined = " ".join(f"{k}={v}" for k, v in os.environ.items())
            assert not any(m in joined for m in ("GIT_PLUS_RUNNER", "SSH_", "TOKEN", "XDG_RUNTIME_DIR", "DBUS", "GITHUB"))
        def test_unprivileged():
            assert os.getuid() == 65534
        def test_host_files_invisible():
            assert not pathlib.Path("/home/gp-runner").exists() and not pathlib.Path("/etc/git-plus-runner").exists()
            assert not pathlib.Path("/etc/ssh").exists() or not any(pathlib.Path("/etc/ssh").glob("*authorized*"))
        def test_no_network_not_even_the_host():
            for target in (("10.0.2.2", 22), ("10.0.2.3", 53), ("1.1.1.1", 443)):
                s = socket.socket(); s.settimeout(3)
                with pytest.raises(OSError):
                    s.connect(target)
        def test_read_only_root_and_dependencies():
            for path in ("/etc/pwn", "/opt/env/pwn", "/in/pwn", "/usr/pwn"):
                with pytest.raises(OSError):
                    open(path, "w")
    '''}
    _, out = submit(tmp_path, files)
    assert out['status'] == 'passed', out


def test_memory_pids_and_disk_limits_are_enforced_remotely(tmp_path):
    files = {'tests/test_a.py': '''
        import os, subprocess, sys
        def test_pids():
            made = 0
            try:
                while made < 5000:
                    subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"]); made += 1
            except OSError:
                pass
            assert made < 5000
        def test_disk():
            try:
                with open("/work/big", "wb") as handle:
                    for _ in range(4096):
                        handle.write(b"x" * (1 << 20))
            except OSError as exc:
                assert exc.errno == 28
                return
            raise AssertionError("tmpfs was not bounded")
    ''', 'tests/test_m.py': 'def test_mem():\n    blocks = [bytearray(64 * 1024 * 1024) for _ in range(100)]\n'}
    _, out = submit(tmp_path, files, timeout=120)
    by_id = {t['id']: t['outcome'] for t in out['tests']}
    assert by_id['tests/test_a.py::test_pids'] == 'passed' and by_id['tests/test_a.py::test_disk'] == 'passed', out
    assert out['status'] == 'failed' and out['counts']['passed'] == 2


def test_timeout_over_ssh_is_reported_and_cleaned(tmp_path):
    _, out = submit(tmp_path, SLEEPER, timeout=10)
    assert out['status'] == 'timeout'


# --- the SSH account is not a shell ----------------------------------------------------------------

@pytest.mark.parametrize('requested', ['id', 'cat /etc/passwd', 'touch /tmp/gp-pwned; echo $0', 'git-plus-runner check', '/bin/sh'])
def test_requested_commands_never_run(requested):
    code, out, _ = exchange(b'', command=RUNNER + [requested], hold=False)
    text = out.decode(errors='replace')
    assert 'root:' not in text and 'uid=' not in text
    assert json.loads(text)['setup_error']['stage'] == 'frame'  # the forced command ran, and only it
    assert vm('test -e /tmp/gp-pwned && echo yes || echo no').strip() == 'no'


def test_no_pty_forwarding_agent_or_subsystems():
    for flags in (['-t', '-t'], ['-L', '127.0.0.1:19999:127.0.0.1:22', '-o', 'ClearAllForwardings=no'], ['-A'], ['-X'], ['-s', 'sftp']):
        done = subprocess.run(RUNNER[:1] + ['-v'] + RUNNER[1:-1] + flags + [RUNNER[-1], 'x'], input=b'', capture_output=True, timeout=30)
        log = done.stderr.decode(errors='replace')
        assert b'root:' not in done.stdout
        assert 'pty-req' not in log or 'PTY allocation request failed' in log or 'Pseudo-terminal' in log or done.returncode != 0 or b'setup_error' in done.stdout
    assert vm('ss -ltn | grep -c 19999 || true').strip() == '0'


def test_only_the_dedicated_key_and_account_are_accepted(tmp_path):
    other = tmp_path / 'other'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
    for command in (ssh_command(key='../' + str(other).lstrip('/')), ssh_command(user='root'), ssh_command(user='vmadmin', key='runner')):
        done = subprocess.run(command + ['id'], input=b'', capture_output=True, timeout=30)
        assert done.returncode == 255 and b'uid=' not in done.stdout


def test_runner_account_cannot_change_its_own_ssh_policy():
    out = as_runner('touch /etc/ssh/git-plus-runner.authorized_keys 2>&1; touch /usr/local/libexec/x 2>&1; touch /opt/git-plus-runner/venv/x 2>&1; touch /etc/git-plus-runner/runner.env 2>&1')
    assert out.count('Permission denied') == 4


# --- protocol abuse ----------------------------------------------------------------------------------

def test_invalid_protocol_messages_are_rejected_without_execution():
    run_id = new_run_id()
    good = header_for(run_id)
    cases = {
        'garbage': (b'\xff' * 64, 'frame'),
        'zero_header': ((0).to_bytes(4, 'big') + b'x' * 20, 'frame'),
        'huge_header_length': ((10 ** 9).to_bytes(4, 'big') + b'{}', 'frame'),
        'not_json': ((5).to_bytes(4, 'big') + b'[[[[[' + (0).to_bytes(8, 'big'), 'frame'),
        'wrong_schema': (raw_frame({**good, 'schema': 9}), 'validation'),
        'bad_job_id': (raw_frame({**good, 'job_id': '../../etc'}), 'validation'),
        'unknown_profile': (raw_frame({**good, 'profile': 'shell'}), 'validation'),
        'selector_flag': (raw_frame({**good, 'selectors': ['--collect-only', 'tests']}), 'validation'),
        'selector_escape': (raw_frame({**good, 'selectors': ['tests/../../etc']}), 'validation'),
        'unpinned_requirements': (raw_frame({**good, 'requirements': 'pytest\nevil @ https://example.invalid/x.whl\n'}), 'validation'),
        'timeout_over_limit': (raw_frame({**good, 'timeout_seconds': 10 ** 6}), 'validation'),
    }
    for name, (frame, stage) in cases.items():
        code, out, _ = exchange(frame)
        result = json.loads(out)
        assert result['setup_error']['stage'] == stage, (name, result)
        assert result['cleanup']['job_workspace_removed'] is True, name
    assert not containers()


def test_oversized_artifact_is_refused_before_it_is_read():
    started = time.monotonic()
    code, out, _ = exchange(raw_frame(header_for(new_run_id()), b'', claimed_length=10 ** 12))
    assert json.loads(out)['setup_error']['stage'] == 'validation' and time.monotonic() - started < 20


def test_interrupted_transfer_is_a_clean_setup_error():
    artifact = tar_bytes(OK)
    frame = raw_frame(header_for(new_run_id()), artifact)
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(frame[:-len(artifact) // 2])
    proc.stdin.flush()
    proc.stdin.close()  # the client vanishes mid-transfer
    out = proc.stdout.read()
    assert json.loads(out)['setup_error']['stage'] == 'frame'


# --- connection loss, duplicates, runner failure --------------------------------------------------

def test_client_disconnect_mid_run_stops_the_container_and_removes_the_workspace(tmp_path):
    run_id = new_run_id()
    spool = tmp_path / run_id
    spool.mkdir()
    (spool / 'source.tar').write_bytes(tar_bytes(SLEEPER))
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc.stdin.write(raw_frame(header_for(run_id, timeout=120), (spool / 'source.tar').read_bytes()))
    proc.stdin.flush()
    assert wait_until(lambda: any(n.startswith('gp-run-') for n in containers()), 60), 'the job never started'
    started = time.monotonic()
    proc.send_signal(signal.SIGKILL)  # the Git Plus side dies abruptly: no clean SSH shutdown
    proc.wait()
    assert wait_until(lambda: not containers() and not workspaces(), 30)
    assert time.monotonic() - started < 30  # seconds, not the 180 s hard timeout
    again = submit(tmp_path, OK)[1]  # reconnection after the loss works
    assert again['status'] == 'passed'


def test_duplicate_submission_is_rejected_and_does_not_disturb_the_first(tmp_path):
    run_id = new_run_id()
    spool = tmp_path / run_id
    spool.mkdir()
    artifact = tar_bytes({'tests/test_a.py': 'import time\ndef test_a(): time.sleep(8)\n'})
    first = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    first.stdin.write(raw_frame(header_for(run_id, timeout=60), artifact))
    first.stdin.flush()
    assert wait_until(lambda: any(n.startswith('gp-run-') for n in containers()), 60)
    code, out, _ = exchange(raw_frame(header_for(run_id, timeout=60), artifact))
    assert json.loads(out)['setup_error']['stage'] == 'duplicate'
    assert any(n.startswith('gp-run-') for n in containers())  # the first job still has its container
    result = json.loads(first.stdout.read())
    first.stdin.close()
    assert classify(result, ('tests',), SPEC, ())['status'] == 'passed'


def test_runner_process_killed_with_sigkill_leaves_no_orphan_after_housekeeping(tmp_path):
    run_id = new_run_id()
    artifact = tar_bytes(SLEEPER)
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc.stdin.write(raw_frame(header_for(run_id, timeout=120), artifact))
    proc.stdin.flush()
    assert wait_until(lambda: any(n.startswith('gp-run-') for n in containers()), 60)
    vm('sudo pkill -9 -u gp-runner -f "bin/git-plus-runner$"')  # the runner itself dies; nothing can clean up
    survivors = containers()
    assert survivors, 'expected the orphan to outlive the SIGKILLed runner (the defect this test documents)'
    report = json.loads(vm('cd /; sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry gc'))
    assert report['orphan_containers_removed'], report
    assert wait_until(lambda: not containers(), 20)
    proc.kill()
    # the SIGKILLed runner's workspace is swept by the next housekeeping run once its lock is seen free
    assert wait_until(lambda: not workspaces() or bool(vm('cd /; sudo -u gp-runner /usr/local/libexec/git-plus-runner-entry gc')) and not workspaces(), 60)


def test_runner_unavailable_is_a_setup_error_not_a_hang(tmp_path):
    started = time.monotonic()
    _, out = submit(tmp_path, OK, command=ssh_command(port=1))
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'runner' and time.monotonic() - started < 30


def test_host_key_mismatch_is_refused_before_any_job_data_is_sent(tmp_path):
    wrong = tmp_path / 'wrong_known_hosts'
    key = tmp_path / 'wrong'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    wrong.write_text(f'[{HOST}]:{PORT} ' + ' '.join((key.parent / 'wrong.pub').read_text().split()[:2]) + '\n')
    _, out = submit(tmp_path, OK, command=ssh_command(known_hosts=str(wrong)))
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'runner'
    assert 'HOST IDENTIFICATION' in json.dumps(out) or 'Host key verification failed' in json.dumps(out)
    empty = tmp_path / 'empty'
    empty.write_text('')
    _, unknown = submit(tmp_path, OK, command=ssh_command(known_hosts=str(empty)))
    assert unknown['status'] == 'setup_error'


def _fake_server(behavior):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)

    def serve():
        listener.settimeout(20)
        try:
            while True:
                conn, _ = listener.accept()
                if behavior == 'garbage':
                    conn.sendall(b'this is not ssh\r\n\x00\x01' * 5)
                elif behavior == 'http':
                    conn.sendall(b'HTTP/1.1 200 OK\r\n\r\nhello')
                elif behavior == 'hang':
                    time.sleep(15)
                conn.close()
        except OSError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    return listener


@pytest.mark.parametrize('behavior', ['garbage', 'http', 'hang'])
def test_unexpected_server_behavior_fails_closed(tmp_path, behavior):
    listener = _fake_server(behavior)
    try:
        port = listener.getsockname()[1]
        started = time.monotonic()
        command = ssh_command(host='127.0.0.1', port=port, extra=['-o', 'ConnectTimeout=3'])
        _, out = submit(tmp_path, OK, command=command)
        assert out['status'] == 'setup_error' and out['failure']['kind'] == 'runner'
        assert time.monotonic() - started < 25
    finally:
        listener.close()


# --- Git Plus service over the real transport ---------------------------------------------------------

def test_result_survives_service_restart_and_client_reconnection(tmp_path):
    cfg = RunSettings(runner_command=tuple(RUNNER), policies_path='x', data_dir=str(tmp_path / 'data'), max_queued=3,
                      default_timeout=60, min_free_bytes=0)
    service = JobService(cfg)
    run_id = new_run_id()
    service.reserve()
    spool = service.spool_dir(run_id)
    (spool / 'source.tar').write_bytes(tar_bytes(OK))
    now = service.clock()
    record = {'schema': 1, 'run_id': run_id, 'owner': 'local', 'repo': 'owner/repo', 'pr': 1, 'mode': 'head', 'profile': 'python-pytest',
              'status': 'queued', 'selectors': ['tests'], 'requirements_sha256': SPEC, 'overlay': {}, 'expires_at': now + 3600,
              'timestamps': {'created_at': now, 'started_at': None, 'finished_at': None}, 'result': None}
    service.enqueue(record, QueuedJob(run_id, spool, header_for(run_id), 60))
    assert wait_until(lambda: (service.store.load(run_id) or {}).get('status') in {'passed', 'failed', 'setup_error'}, 90)
    service.shutdown()
    reborn = JobService(cfg)  # a new process: results come from disk, not from the old connection
    stored = reborn.get(run_id)
    assert stored['status'] == 'passed' and stored['owner'] == 'local'
    reborn.shutdown()


# --- dependency cache and disk pressure on the real runner host ---------------------------------------

ENVS = '/home/gp-runner/.local/state/git-plus-runner/envs'


def env_dirs():
    return sorted(n for n in as_runner(f'ls -A {ENVS} 2>/dev/null').split() if not n.startswith('.') and '.' not in n)


def reset_envs():
    as_runner(f'rm -rf {ENVS}/* {ENVS}/.prep-* {ENVS}/.evict-* 2>/dev/null; true')


def reqs(extra=''):
    return validate_requirements('pytest==8.3.5\n' + extra)


def test_cache_cold_warm_invalidation_and_corruption_recovery(tmp_path):
    reset_envs()
    base = reqs()
    cold = submit(tmp_path, OK, requirements=base)[1]['environment']
    warm = submit(tmp_path, OK, requirements=base)[1]['environment']
    assert (cold['cache_status'], warm['cache_status']) == ('miss', 'hit') and cold['cache_key'] == warm['cache_key']
    assert cold['prepare_seconds'] > 0 and warm['prepare_seconds'] == 0 and cold['size_bytes'] > 1_000_000
    changed = submit(tmp_path, OK, requirements=reqs('six==1.17.0\n'))[1]['environment']
    assert changed['cache_status'] == 'miss' and changed['cache_key'] != cold['cache_key']
    assert sorted(env_dirs()) == sorted({cold['cache_key'], changed['cache_key']})  # the old one is kept for other PRs
    as_runner(f'rm -f {ENVS}/{cold["cache_key"]}/.gp-ready')  # a half-written or damaged environment
    healed = submit(tmp_path, OK, requirements=base)[1]
    assert healed['status'] == 'passed' and healed['environment']['cache_status'] == 'miss'  # never reused, rebuilt instead


def test_failed_preparation_is_cleaned_and_reported(tmp_path):
    reset_envs()
    bad = reqs('definitely-not-a-package-gp==9.9.9\n')
    _, out = submit(tmp_path, OK, requirements=bad)
    assert out['status'] == 'setup_error' and out['failure']['kind'] == 'prepare'
    assert env_dirs() == [] and as_runner(f'ls -A {ENVS} | grep -c "^.prep-" || true').strip() == '0'


def test_concurrent_jobs_prepare_one_environment(tmp_path):
    reset_envs()
    results = {}

    def go(name):
        sub = tmp_path / name
        sub.mkdir()
        results[name] = submit(sub, OK, requirements=reqs('six==1.17.0\n'))[1]['environment']['cache_status']
    threads = [threading.Thread(target=go, args=(n,)) for n in ('a', 'b', 'c')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ['hit_after_wait', 'hit_after_wait', 'miss'] and len(env_dirs()) == 1


def test_disk_pressure_evicts_idle_environments_but_never_one_in_use(tmp_path):
    reset_envs()
    held = reqs()
    other = reqs('six==1.17.0\n')
    submit(tmp_path, OK, requirements=other)  # idle environment, oldest
    run_id = new_run_id()
    spool = tmp_path / run_id
    spool.mkdir()
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc.stdin.write(raw_frame(header_for(run_id, timeout=120, requirements=held), tar_bytes({'tests/test_a.py': 'import time\ndef test_a(): time.sleep(12)\n'})))
    proc.stdin.flush()
    assert wait_until(lambda: any(n.startswith('gp-run-') for n in containers()), 60)  # `held` is now mounted by a live job
    assert len(env_dirs()) == 2
    vm("sudo sh -c 'echo GIT_PLUS_RUNNER_ENV_BUDGET_MIB=1 >> /etc/git-plus-runner/runner.env'")
    try:
        third = submit(tmp_path, OK, requirements=reqs('idna==3.10\n'))[1]  # budget exceeded: collect() must pick victims
    finally:
        vm("sudo sed -i '/ENV_BUDGET_MIB=1$/d' /etc/git-plus-runner/runner.env")
    evicted = {e['key'] for e in third['storage']['evicted']}
    keys = env_dirs()
    result = json.loads(proc.stdout.read())
    proc.stdin.close()
    assert classify(result, ('tests',), requirements_digest(held), ())['status'] == 'passed'  # the running job was untouched
    assert evicted and third['storage']['protected_in_use'] >= 1 and third['status'] == 'passed'
    assert result['environment']['cache_key'] not in evicted and result['environment']['cache_key'] in keys


def test_housekeeping_timer_is_installed_and_scheduled():
    assert 'git-plus-runner-gc.timer' in vm('systemctl list-timers --no-legend | cat')
    assert vm('systemctl is-enabled git-plus-runner-gc.timer').strip() == 'enabled'


# --- network policy and silent partitions -----------------------------------------------------------------

EGRESS = '''import socket
for name, (host, port) in {"public": ("pypi.org", 443), "gateway": ("10.0.2.2", 22), "private": ("192.168.0.1", 80), "metadata": ("169.254.169.254", 80)}.items():
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, port)); print(name, "reachable")
    except OSError:
        print(name, "blocked")
'''


def test_runner_account_egress_reaches_the_package_index_but_no_internal_network(tmp_path):
    local = tmp_path / 'egress.py'
    local.write_text(EGRESS)
    subprocess.run(['scp', '-q', '-i', f'{KEYS}/admin', '-o', 'IdentitiesOnly=yes', '-o', f'UserKnownHostsFile={KEYS}/known_hosts', '-P', PORT,
                    str(local), f'vmadmin@{HOST}:/tmp/egress.py'], check=True)
    vm('chmod 644 /tmp/egress.py')
    expected = {'public': 'reachable', 'gateway': 'blocked', 'private': 'blocked', 'metadata': 'blocked'}
    assert dict(line.split() for line in as_runner('python3 /tmp/egress.py').splitlines()) == expected
    # The preparation container is the real consumer of this policy: its traffic leaves under the account's uid.
    image = vm("sudo grep '^GIT_PLUS_RUNNER_IMAGE=' /etc/git-plus-runner/runner.env").strip().split('=', 1)[1]
    in_container = as_runner(f'podman run --rm --pull=never -v /tmp/egress.py:/e.py:ro --entrypoint python {image} /e.py')
    assert dict(line.split() for line in in_container.splitlines()) == expected


def test_silent_network_partition_is_detected_by_the_server_and_cleaned_up():
    run_id = new_run_id()
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc.stdin.write(raw_frame(header_for(run_id, timeout=300), tar_bytes(SLEEPER)))
    proc.stdin.flush()
    assert wait_until(lambda: any(n.startswith('gp-run-') for n in containers()), 60)
    proc.send_signal(signal.SIGSTOP)  # no FIN, no RST: the client simply stops answering keepalives
    try:
        assert wait_until(lambda: not containers() and not workspaces(), 120)  # ClientAliveInterval 15 x CountMax 3, plus cleanup
    finally:
        proc.send_signal(signal.SIGCONT)
        proc.kill()


def test_authorized_key_is_bound_to_the_git_plus_source_address():
    original = vm('sudo cat /etc/ssh/git-plus-runner.authorized_keys')
    assert 'restrict,command="/usr/local/libexec/git-plus-runner-entry"' in original and 'from="' in original
    vm("sudo sed -i 's/from=\"[^\"]*\"/from=\"203.0.113.9\"/' /etc/ssh/git-plus-runner.authorized_keys")
    try:
        done = subprocess.run(RUNNER + ['id'], input=b'', capture_output=True, timeout=30)
        assert done.returncode == 255 and b'Permission denied' in done.stderr
    finally:
        vm("sudo tee /etc/ssh/git-plus-runner.authorized_keys >/dev/null <<'EOF'\n" + original + "EOF")
    assert vm('sudo cat /etc/ssh/git-plus-runner.authorized_keys') == original
