"""Runner protocol and housekeeping behavior that needs no container engine."""
import os
import sys
import threading
import time

import pytest

from git_plus import dep_cache, sandbox
from git_plus.procs import run_bounded


def pipe_reader(deadline):
    read_end, write_end = os.pipe()
    return sandbox.FdReader(read_end, deadline), read_end, write_end


def test_frame_deadline_cuts_off_a_stalled_client():
    reader, r, w = pipe_reader(0.5)
    os.write(w, b'\x00\x00')  # half of a length prefix, then silence
    started = time.monotonic()
    with pytest.raises(sandbox.SetupError) as raised:
        sandbox._read_exact(reader, 4)
    assert raised.value.stage == 'frame' and 'Timed out' in raised.value.message and time.monotonic() - started < 5
    os.close(r); os.close(w)


def test_frame_reader_reassembles_split_writes_and_detects_truncation():
    reader, r, w = pipe_reader(5)

    def trickle():
        for part in (b'ab', b'cd', b'ef'):
            os.write(w, part)
            time.sleep(0.05)
        os.close(w)
    threading.Thread(target=trickle).start()
    assert sandbox._read_exact(reader, 6) == b'abcdef'
    with pytest.raises(sandbox.SetupError, match='Truncated'):
        sandbox._read_exact(reader, 1)
    os.close(r)


def test_config_file_must_be_root_owned_and_only_runner_keys(tmp_path):
    path = tmp_path / 'runner.env'
    path.write_text('GIT_PLUS_RUNNER_IMAGE=docker.io/library/python@sha256:' + 'a' * 64 + '\n')
    if os.getuid() != 0:  # the test account is not root: the trust check must refuse the file
        with pytest.raises(sandbox.SetupError, match='owned by root'):
            sandbox.load_config_file(str(path))


def test_config_file_parsing_rejects_foreign_keys(tmp_path, monkeypatch):
    path = tmp_path / 'runner.env'
    path.write_text('# comment\nGIT_PLUS_RUNNER_CPUS="1"\nPATH=/evil\n')
    real_stat = os.stat
    monkeypatch.setattr(sandbox.os, 'stat', lambda p: type('S', (), {'st_uid': 0, 'st_mode': 0o100644})() if str(p) == str(path) else real_stat(p))
    with pytest.raises(sandbox.SetupError, match='Invalid runner configuration line 3'):
        sandbox.load_config_file(str(path))
    path.write_text('GIT_PLUS_RUNNER_CPUS="1"\n  GIT_PLUS_RUNNER_PIDS = 64\n')
    assert sandbox.load_config_file(str(path)) == {'GIT_PLUS_RUNNER_CPUS': '1', 'GIT_PLUS_RUNNER_PIDS': '64'}


def test_held_stdin_signals_the_child_only_when_the_parent_is_gone():
    """With hold_stdin the child sees EOF only after the parent closes it, which is what a lost SSH session looks like."""
    code = "import sys\nsys.stdin.buffer.read(3)\nsys.stdout.write('got3\\n');sys.stdout.flush()\nsys.stdout.write('eof' if sys.stdin.buffer.read() == b'' else 'more')"
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    result = run_bounded([sys.executable, '-c', code], env=os.environ, timeout=30, max_stdout=1000,
                         stdin_chunks=iter([b'abc']), cancel=cancel, hold_stdin=True)
    assert result.canceled and b'got3' in result.stdout and b'eof' not in result.stdout and time.monotonic() - started < 10
    done = run_bounded([sys.executable, '-c', code], env=os.environ, timeout=30, max_stdout=1000, stdin_chunks=iter([b'abc']))
    assert done.stdout.endswith(b'eof')  # the old behavior is still available


def test_dead_job_workspaces_are_swept_quickly_but_live_ones_never(tmp_path):
    jobs = tmp_path / 'jobs'
    live, dead, starting = jobs / 'job-1-aaaaaaaa', jobs / 'job-2-bbbbbbbb', jobs / 'job-3-cccccccc'
    for d in (live, dead, starting):
        d.mkdir(parents=True)
        (d / 'f').write_text('x')
    holder = dep_cache._try_lock(live / '.lock', exclusive=True)  # a live runner holds its workspace lock
    (dead / '.lock').write_text('')  # created and then abandoned: its runner died
    old = time.time() - 60
    for d in (live, dead):
        os.utime(d, (old, old))
    removed = dep_cache.sweep_debris(tmp_path, now=time.time())
    assert removed == 1 and live.exists() and not dead.exists()
    assert starting.exists()  # no lock file and younger than the long grace: may still be starting
    os.close(holder)
