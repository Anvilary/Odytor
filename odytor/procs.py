"""Bounded subprocess execution with time, output and cancellation limits (standard library only)."""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence


@dataclass
class ProcResult:
    returncode: int | None
    stdout: bytes
    stderr_tail: bytes
    timed_out: bool = False
    canceled: bool = False
    overflowed: bool = False


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run_bounded(argv: Sequence[str], *, env: Mapping[str, str], timeout: float, max_stdout: int,
                stdin_chunks: Iterable[bytes] | None = None, cancel: threading.Event | None = None,
                grace: float = 5.0, stderr_limit: int = 8192, hold_stdin: bool = False,
                on_stderr_line: Callable[[str], None] | None = None) -> ProcResult:
    """Run without a shell; never buffer more than max_stdout bytes; always reap the process group.

    hold_stdin keeps the child's stdin open after the chunks are written, so its EOF means this process is gone.
    on_stderr_line is an optional callback called for each complete line emitted on stderr.
    """
    proc = subprocess.Popen(list(argv), env=dict(env), stdin=subprocess.PIPE if stdin_chunks is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    out = bytearray()
    err: collections.deque[bytes] = collections.deque()
    err_buf = bytearray()
    state = {"overflow": False, "err_len": 0}

    def read_out() -> None:
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            room = max_stdout - len(out)
            if len(chunk) > room:
                state["overflow"] = True
            out.extend(chunk[:room])

    def read_err() -> None:
        for chunk in iter(lambda: proc.stderr.read(8192), b""):
            err.append(chunk)
            state["err_len"] += len(chunk)
            while state["err_len"] - len(err[0]) >= stderr_limit:
                state["err_len"] -= len(err.popleft())
            if on_stderr_line is not None:
                err_buf.extend(chunk)
                while b"\n" in err_buf:
                    line_bytes, _, rest = err_buf.partition(b"\n")
                    err_buf[:] = rest
                    try:
                        on_stderr_line(line_bytes.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
        if on_stderr_line is not None and err_buf:
            try:
                on_stderr_line(err_buf.decode("utf-8", errors="replace"))
            except Exception:
                pass

    def feed() -> None:
        try:
            for chunk in stdin_chunks or ():
                proc.stdin.write(chunk)
            proc.stdin.flush()
            if not hold_stdin:
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    if stdin_chunks is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = canceled = False
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                canceled = True
            elif time.monotonic() > deadline:
                timed_out = True
            elif state["overflow"]:
                pass
            else:
                time.sleep(0.05)
                continue
            proc.terminate()
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                pass
            break
    finally:
        _kill_group(proc)
        proc.wait()
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        for thread in threads:
            thread.join(timeout=5)
    return ProcResult(proc.returncode, bytes(out), b"".join(err)[-stderr_limit:], timed_out, canceled, state["overflow"])
