"""In-sandbox entry point: unpack the source artifact, run the fixed invocation, report.

Standard library only; copied beside the job inputs. The container has no network and no
credentials. Everything this module reports is evidence from inside the sandbox, which
the code under test can influence, and is validated by the host before use.
"""
import base64
import collections
import glob
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time

MARK = "ODYTOR_HARNESS_RESULT:"
WORK, ENV_SITE, INPUT = "/work", "/opt/env/site", "/in"
EVENTS = "/tmp/gp/events.jsonl"
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_TESTS = 200
MAX_TEXT = 200
LOG_HEAD, LOG_TAIL = 4096, 8192

_ODYTOR_REPORTER_JS = """class OdyTorReporter {
  onBegin(config, suite) {
    try {
      const total = suite.allTests().length;
      process.stderr.write('\\nODYTOR_PROGRESS:' + JSON.stringify({
        type: 'collection',
        phase: 'executing_tests',
        total: total
      }) + '\\n');
    } catch (e) {}
  }
  onTestBegin(test) {
    try {
      const fullTitle = typeof test.titlePath === 'function' ? test.titlePath().join(' > ') : (test.title || '');
      process.stderr.write('\\nODYTOR_PROGRESS:' + JSON.stringify({
        type: 'test_start',
        phase: 'executing_tests',
        id: fullTitle,
        title: test.title || fullTitle,
        file: (test.location && test.location.file) || ''
      }) + '\\n');
    } catch (e) {}
  }
  onTestEnd(test, result) {
    try {
      const fullTitle = typeof test.titlePath === 'function' ? test.titlePath().join(' > ') : (test.title || '');
      process.stderr.write('\\nODYTOR_PROGRESS:' + JSON.stringify({
        type: 'test_end',
        phase: 'executing_tests',
        id: fullTitle,
        title: test.title || fullTitle,
        outcome: result.status,
        duration_ms: Math.round(result.duration || 0),
        retry: result.retry || 0
      }) + '\\n');
    } catch (e) {}
  }
  onEnd(result) {
    try {
      process.stderr.write('\\nODYTOR_PROGRESS:' + JSON.stringify({
        type: 'phase',
        phase: 'finalizing',
        status: result.status
      }) + '\\n');
    } catch (e) {}
  }
}
module.exports = OdyTorReporter;
"""


def _emit_progress(**event):
    try:
        event.setdefault("ts", round(time.time(), 3))
        sys.stderr.write("ODYTOR_PROGRESS:" + json.dumps(event) + "\n")
        sys.stderr.flush()
    except Exception:
        pass



def _extract(limit_files, limit_bytes):
    count = total = 0
    skipped = []

    def keep_safe(member, dest):
        try:
            return tarfile.data_filter(member, dest)
        except tarfile.FilterError:
            if member.issym() or member.islnk():
                skipped.append(member.name)
                return None
            raise

    with tarfile.open(os.path.join(INPUT, "source.tar")) as archive:
        members = []
        for member in archive:
            count += 1
            total += max(member.size, 0)
            if count > limit_files or total > limit_bytes:
                raise RuntimeError("source artifact exceeds the extraction limits")
            members.append(member)
        archive.extractall(WORK, members=members, filter=keep_safe)
    return count, total, skipped


def _read_events():
    events, malformed, size = [], 0, 0
    try:
        with open(EVENTS, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                size += len(line)
                if size > MAX_EVENT_BYTES:
                    malformed += 1
                    break
                try:
                    item = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(item, dict) and _valid_pytest_event(item):
                    events.append(item)
                else:
                    malformed += 1
    except OSError:
        pass
    return events, malformed


def _valid_pytest_event(event):
    kind = event.get("e")
    if kind == "collected":
        ids = event.get("ids")
        count = event.get("n")
        return (type(count) is int and isinstance(ids, list) and 0 <= len(ids) <= count
                and all(isinstance(nodeid, str) and nodeid for nodeid in ids))
    if kind == "finish":
        return type(event.get("exit")) is int
    if kind == "collect_error":
        return isinstance(event.get("id"), str)
    return (kind == "report" and isinstance(event.get("id"), str)
            and event.get("w") in ("setup", "call", "teardown")
            and event.get("o") in ("passed", "failed", "skipped"))


def _num(value, high=1e12):
    try:
        n = float(value)
        return min(max(n, 0.0), high) if n == n else 0.0
    except (TypeError, ValueError):
        return 0.0


def _matches(selector, nodeid):
    if "::" not in selector and not selector.endswith(".py"):
        return nodeid.startswith(selector.rstrip("/") + "/")
    return nodeid == selector or nodeid.startswith((selector + "::", selector + "["))


def _coverage(selectors, collected, tests):
    ids = (collected or {}).get("ids", [])
    ran = {t["id"] for t in tests if t["outcome"] in {"passed", "failed", "error", "xfailed", "xpassed"}}
    return [{"selector": sel, "collected": sum(_matches(sel, i) for i in ids),
             "executed": sum(_matches(sel, i) for i in ran)} for sel in selectors]


def _summarize(events):
    collected, collect_errors, finish = None, [], None
    phases = collections.OrderedDict()
    for event in events:
        kind = event.get("e")
        if kind == "collected" and collected is None and isinstance(event.get("n"), int):
            ids = event.get("ids") if isinstance(event.get("ids"), list) else []
            collected = {"count": event["n"], "ids": [i[:MAX_TEXT] for i in ids if isinstance(i, str)]}
        elif kind == "collect_error":
            collect_errors.append({"id": str(event.get("id", ""))[:MAX_TEXT], "message": str(event.get("msg", ""))[:MAX_TEXT]})
        elif kind == "finish" and isinstance(event.get("exit"), int):
            finish = event["exit"]
        elif kind == "report" and isinstance(event.get("id"), str):
            phases.setdefault(event["id"], []).append(event)
    tests = []
    for nodeid, reports in phases.items():
        outcome, message, duration = None, "", 0.0
        for rep in reports:
            duration += _num(rep.get("d"))
            state, when, xfail = rep.get("o"), rep.get("w"), bool(rep.get("xfail"))
            if state == "failed":
                outcome = "failed" if when == "call" and outcome != "error" else "error"
                message = message or str(rep.get("msg", ""))
            elif state == "skipped" and outcome is None:
                outcome = "xfailed" if xfail else "skipped"
                message = str(rep.get("msg", ""))
            elif state == "passed" and when == "call" and outcome is None:
                outcome = "xpassed" if xfail else "passed"
        expected = ["setup", "call", "teardown"] if reports[0].get("o") == "passed" else ["setup", "teardown"]
        if [rep.get("w") for rep in reports] != expected:
            outcome = "incomplete"
        tests.append({"id": nodeid[:MAX_TEXT], "outcome": outcome or "incomplete", "duration": round(duration, 4),
                      "message": message[:MAX_TEXT]})
    return collected, collect_errors, finish, tests


def _counts(tests, collected):
    counts = collections.Counter(item["outcome"] for item in tests)
    reported = len(tests)
    return {"collected": collected["count"] if collected else 0, "reported": reported,
            "passed": counts["passed"], "failed": counts["failed"], "errors": counts["error"],
            "skipped": counts["skipped"], "xfailed": counts["xfailed"], "xpassed": counts["xpassed"],
            "incomplete": counts["incomplete"],
            "executed": counts["passed"] + counts["failed"] + counts["error"] + counts["xfailed"] + counts["xpassed"],
            "not_run": max((collected["count"] if collected else 0) - reported + counts["incomplete"], 0)}


RETAIN_HEAD, RETAIN_TAIL = 96 * 1024, 160 * 1024   # bounded copy of the whole log, returned as an artifact when the snippets are not enough
_LOGS = []


class _Log:
    """Constant-memory log capture: snippets (first and last bytes plus a total) and a larger bounded retained copy."""

    def __init__(self):
        self.head, self.tail, self.total = b"", collections.deque(), 0
        self._tail_len = 0
        self.keep_head, self.keep_tail, self._keep_tail_len = bytearray(), collections.deque(), 0
        _LOGS.append(self)

    def feed(self, chunk):
        self.total += len(chunk)
        rest = chunk
        if len(self.keep_head) < RETAIN_HEAD:
            take = RETAIN_HEAD - len(self.keep_head)
            self.keep_head += rest[:take]
            rest = rest[take:]
        if rest:
            self.keep_tail.append(rest)
            self._keep_tail_len += len(rest)
            while self._keep_tail_len - len(self.keep_tail[0]) >= RETAIN_TAIL:
                self._keep_tail_len -= len(self.keep_tail.popleft())
        if len(self.head) < LOG_HEAD:
            take = LOG_HEAD - len(self.head)
            self.head += chunk[:take]
            chunk = chunk[take:]
        if chunk:
            self.tail.append(chunk)
            self._tail_len += len(chunk)
            while self._tail_len - len(self.tail[0]) >= LOG_TAIL:
                self._tail_len -= len(self.tail.popleft())

    def render(self):
        tail = b"".join(self.tail)[-LOG_TAIL:]
        truncated = self.total > len(self.head) + len(tail)
        return {"head": self.head.decode("utf-8", "replace"), "tail": tail.decode("utf-8", "replace"),
                "total_bytes": self.total, "truncated": truncated}

    def retained(self):
        """(bytes, complete): the retained copy, and whether it is the entire log."""
        tail = b"".join(self.keep_tail)[-RETAIN_TAIL:]
        complete = self.total <= len(self.keep_head) + len(tail)
        if complete:
            return bytes(self.keep_head) + tail, True
        return bytes(self.keep_head) + b"\n[... %d bytes omitted by the runner ...]\n" % (self.total - len(self.keep_head) - len(tail)) + tail, False


class _ProgressTap:
    """Child output filter: forwards the ODYTOR_PROGRESS lines a child writes into its merged output as live events and
    passes everything else to the log. Without it those lines only ever reached the final log."""

    PREFIX = b"ODYTOR_PROGRESS:"

    def __init__(self, log):
        self.log, self.buf = log, bytearray()

    def feed(self, chunk):
        self.buf += chunk
        while True:
            end = self.buf.find(b"\n")
            if end < 0:
                break
            line = bytes(self.buf[:end + 1])
            del self.buf[:end + 1]
            self._line(line)
        if len(self.buf) > 65536:  # an overlong unterminated line is plain output
            self.log.feed(bytes(self.buf))
            self.buf.clear()

    def _line(self, line):
        if not line.startswith(self.PREFIX):
            self.log.feed(line)
            return
        if len(line) > 16384:
            return
        try:
            event = json.loads(line[len(self.PREFIX):])
            if isinstance(event, dict):
                _emit_progress(**{k: v for k, v in event.items() if isinstance(k, str)})
        except (ValueError, TypeError):
            self.log.feed(line)

    def close(self):
        if self.buf:
            self.log.feed(bytes(self.buf))
            self.buf.clear()


def _attach_log_artifact(report):
    """When the report's log snippets are not the whole log, keep a bounded copy as an artifact that can be fetched."""
    candidates = [lg for lg in _LOGS if lg.total > len(lg.head) + len(b"".join(lg.tail)[-LOG_TAIL:])]
    if not candidates or isinstance(report.get("artifacts"), list) and any(a.get("name") == "run.log" for a in report["artifacts"]):
        return
    log = max(candidates, key=lambda lg: lg.total)
    data, complete = log.retained()
    report.setdefault("artifacts", []).append({"name": "run.log", "kind": "log", "b64": base64.b64encode(data).decode("ascii")})
    if isinstance(report.get("log"), dict):
        report["log"]["retained_bytes"] = len(data)
        report["log"]["retained_complete"] = complete


def _run_tests(spec):
    report = {"harness": 1, "kind": "test", "extract": {"ok": False}}
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    os.makedirs("/tmp/home", exist_ok=True)
    try:
        files, size, skipped = _extract(spec["limits"]["files"], spec["limits"]["bytes"])
        report["extract"] = {"ok": True, "members": files, "bytes": size, "skipped_unsafe_links": skipped[:20],
                             "skipped_unsafe_link_count": len(skipped)}
    except Exception as exc:
        report["error"] = {"stage": "extract", "message": f"{type(exc).__name__}: {exc}"[:400]}
        return report
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/home", "TMPDIR": "/tmp",
           "LANG": "C.UTF-8", "PYTHONPATH": f"{ENV_SITE}:{INPUT}", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUNBUFFERED": "1", "ODYTOR_EVENTS": EVENTS, "ODYTOR_STREAM_EVENTS": "1"}
    log, started = _Log(), time.monotonic()
    report["argv"] = spec["argv"]
    _emit_progress(type="phase", phase="collecting", message="Collecting pytest tests")
    try:
        proc = subprocess.Popen(spec["argv"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        report["error"] = {"stage": "spawn", "message": str(exc)[:400]}
        return report

    tap = _ProgressTap(log)

    def pump():
        for chunk in iter(lambda: proc.stdout.read1(8192), b""):
            tap.feed(chunk)
        tap.close()

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(4.0):
            _emit_progress(type="heartbeat", elapsed_seconds=round(time.monotonic() - started, 1))

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()
    stop_heartbeat.set()
    hb.join(timeout=1)
    reader.join(timeout=5)
    _emit_progress(type="phase", phase="finalizing", message="Finalizing pytest report")
    events, malformed = _read_events()
    collected, collect_errors, finish, tests = _summarize(events)
    if ([event["e"] for event in events].count("collected") > 1
            or [event["e"] for event in events].count("finish") > 1
            or finish is not None and events[-1]["e"] != "finish"):
        malformed += 1
    if collected and collected["count"] == len(collected["ids"]):
        known_ids = set(collected["ids"])
        if len(known_ids) != collected["count"] or any(test["id"] not in known_ids for test in tests):
            malformed += 1
    counts = _counts(tests, collected)
    order = {"failed": 0, "error": 1, "incomplete": 2, "xpassed": 3, "skipped": 4, "xfailed": 5, "passed": 6}
    ranked = sorted(tests, key=lambda item: order.get(item["outcome"], 9))
    report.update({
        "pytest_exit": proc.returncode, "timed_out": timed_out, "finished_event": finish,
        "duration_seconds": round(time.monotonic() - started, 3), "counts": counts,
        "selector_coverage": _coverage(spec["selectors"], collected, tests),
        "overlay_coverage": _coverage(spec.get("overlay_paths", []), collected, tests),
        "ids_truncated": bool(collected and collected["count"] > len(collected["ids"])),
        "collect_errors": collect_errors[:20],
        "tests": ranked[:MAX_TESTS], "tests_total": len(tests), "tests_truncated": len(tests) > MAX_TESTS,
        "malformed_events": malformed, "log": log.render(),
    })
    return report


TAP_FILE = "/tmp/gp/node.tap"
MAX_TAP_BYTES = 8 * 1024 * 1024
_TAP_RESULT = re.compile(r"^(\s*)(not ok|ok) \d+ - (.*?)(?: # (SKIP|TODO)\b.*)?$")
_TAP_SUMMARY = re.compile(r"^# (tests|suites|pass|fail|cancelled|skipped|todo) (\d+)\s*$")


def _parse_tap(path, root=WORK):
    """Counts from the runner's own summary plus per-test names; `summary_found` is false unless every count arrived.

    Node reports a test *file* that loads and exits cleanly as one passing test named after the file, so a file with
    no tests in it would otherwise count as a pass. Top-level entries named after a file that exists in the tested
    tree are file-level: a passing one is excluded from the counts and listed as vacuous; a failing one (the file
    crashed or did not parse) stays a failure.
    """
    counts, tests, errors, malformed, last_failed = {}, [], {}, 0, None
    vacuous = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_TAP_BYTES + 1)
    except OSError:
        return {"summary_found": False, "counts": {}, "tests": [], "tests_total": 0, "malformed_lines": 0, "truncated": False,
                "vacuous_files": []}
    truncated = len(text) > MAX_TAP_BYTES
    for line in text.splitlines():
        summary = _TAP_SUMMARY.match(line)
        if summary:
            counts[summary.group(1)] = int(summary.group(2))
            continue
        result = _TAP_RESULT.match(line)
        if result:
            name = result.group(3)
            top_level = not result.group(1)
            state = "failed" if result.group(2) == "not ok" else "passed"
            if result.group(4) == "SKIP":
                state = "skipped"
            elif result.group(4) == "TODO":
                state = "todo"
            if top_level and state == "passed" and not name.startswith(("/", "..")) and os.path.isfile(os.path.join(root, name)):
                vacuous.append(name[:MAX_TEXT])
                last_failed = None
                continue
            tests.append({"id": name[:MAX_TEXT], "outcome": state, "duration": 0.0, "message": ""})
            last_failed = tests[-1] if state == "failed" else None
        elif last_failed is not None and line.strip().startswith("error:") and not last_failed["message"]:
            last_failed["message"] = line.strip()[len("error:"):].strip().strip("'\"")[:MAX_TEXT]
        elif line.startswith("not ok") or line.startswith("ok "):
            malformed += 1
    needed = {"tests", "pass", "fail", "cancelled", "skipped", "todo"}
    if needed <= set(counts):
        # the summary counted each vacuous file as one passing test; take them back out
        counts["tests"] = max(counts["tests"] - len(vacuous), 0)
        counts["pass"] = max(counts["pass"] - len(vacuous), 0)
    return {"summary_found": needed <= set(counts), "counts": counts, "tests": tests[:MAX_TESTS], "tests_total": len(tests),
            "malformed_lines": malformed, "truncated": truncated, "vacuous_files": vacuous[:20]}


def _run_node(spec):
    report = {"harness": 1, "kind": "node_test", "extract": {"ok": False}}
    os.makedirs(os.path.dirname(TAP_FILE), exist_ok=True)
    os.makedirs("/tmp/home", exist_ok=True)
    try:
        files, size, skipped = _extract(spec["limits"]["files"], spec["limits"]["bytes"])
        report["extract"] = {"ok": True, "members": files, "bytes": size, "skipped_unsafe_links": skipped[:20],
                             "skipped_unsafe_link_count": len(skipped)}
    except Exception as exc:
        report["error"] = {"stage": "extract", "message": f"{type(exc).__name__}: {exc}"[:400]}
        return report
    report["selector_exists"] = []
    for selector in spec["selectors"]:
        target = os.path.join(WORK, selector.rstrip("/"))
        report["selector_exists"].append({"selector": selector, "exists": os.path.exists(target),
                                          "is_dir": os.path.isdir(target)})
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/home", "TMPDIR": "/tmp",
           "LANG": "C.UTF-8", "NODE_ENV": "test", "CI": "1", "NO_COLOR": "1"}
    log, started = _Log(), time.monotonic()
    report["argv"] = spec["argv"]
    _emit_progress(type="phase", phase="executing_tests", message="Executing node tests")
    try:
        proc = subprocess.Popen(spec["argv"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        report["error"] = {"stage": "spawn", "message": str(exc)[:400]}
        return report

    def pump():
        written = 0
        tap_buf = bytearray()
        with open(TAP_FILE, "wb") as sink:
            for chunk in iter(lambda: proc.stdout.read(8192), b""):
                log.feed(chunk)
                if written < MAX_TAP_BYTES + 1:
                    sink.write(chunk)
                    written += len(chunk)
                tap_buf.extend(chunk)
                while b"\n" in tap_buf:
                    line_bytes, _, rest = tap_buf.partition(b"\n")
                    tap_buf[:] = rest
                    line_str = line_bytes.decode("utf-8", "replace")
                    m = _TAP_RESULT.match(line_str)
                    if m:
                        t_name = m.group(3)
                        t_state = "failed" if m.group(2) == "not ok" else "passed"
                        if m.group(4) == "SKIP":
                            t_state = "skipped"
                        _emit_progress(type="test_end", phase="executing_tests", id=t_name[:MAX_TEXT],
                                       title=t_name[:MAX_TEXT], outcome=t_state, duration_ms=0, retry=0)

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(4.0):
            _emit_progress(type="heartbeat", elapsed_seconds=round(time.monotonic() - started, 1))

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()
    stop_heartbeat.set()
    hb.join(timeout=1)
    reader.join(timeout=5)
    _emit_progress(type="phase", phase="finalizing", message="Finalizing node report")
    tap = _parse_tap(TAP_FILE)
    order = {"failed": 0, "skipped": 1, "todo": 2, "passed": 3}
    report.update({"exit": proc.returncode, "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 3),
                   "tap": {"summary_found": tap["summary_found"], "counts": tap["counts"], "malformed_lines": tap["malformed_lines"],
                           "truncated": tap["truncated"], "vacuous_files": tap["vacuous_files"]},
                   "tests": sorted(tap["tests"], key=lambda t: order.get(t["outcome"], 9)), "tests_total": tap["tests_total"],
                   "log": log.render()})
    return report


def _prep(spec):
    report = {"harness": 1, "kind": "prep", "python": platform.python_version(), "machine": platform.machine()}
    os.makedirs("/tmp/home", exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.startswith("PIP_") or k in {"PATH", "LANG"}}
    env.update(HOME="/tmp/home", TMPDIR="/tmp", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
    install = [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--target", ENV_SITE,
               "-r", os.path.join(INPUT, "requirements.txt")]
    output = b""
    try:
        result = subprocess.run(install, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=spec["timeout"])
        output, code = result.stdout, result.returncode
        if code == 0:
            check = subprocess.run([sys.executable, "-m", "pytest", "--version"],
                                   env={**env, "PYTHONPATH": ENV_SITE}, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            output += check.stdout
            code = check.returncode
        report["exit"] = code
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        report["exit"], report["timed_out"] = None, True
    report["output_tail"] = output[-4096:].decode("utf-8", "replace")
    return report


_DIAG_LINE = re.compile(
    r"^(?P<file>[A-Za-z0-9_./+-]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:(?P<severity>error|warning|fatal error|note|info):\s*)?(?:(?P<code>[A-Za-z0-9_\[\]-]+)\s+)?(?P<message>.+)$"
)


def _parse_diagnostics(text, max_items=50):
    items = []
    for line in text.splitlines():
        line_str = line.strip()
        m = _DIAG_LINE.match(line_str)
        if m:
            d = m.groupdict()
            items.append({
                "file": d["file"],
                "line": int(d["line"]),
                "col": int(d["col"]) if d["col"] else None,
                "severity": d["severity"] or "error",
                "code": d["code"] or "",
                "message": d["message"].strip()[:200],
            })
            if len(items) >= max_items:
                break
    return items


def _run_cmd(spec):
    report = {"harness": 1, "kind": spec.get("kind", "command"), "profile": spec.get("profile", ""), "extract": {"ok": False}}
    os.makedirs("/tmp/home", exist_ok=True)
    try:
        files, size, skipped = _extract(spec["limits"]["files"], spec["limits"]["bytes"])
        report["extract"] = {"ok": True, "members": files, "bytes": size, "skipped_unsafe_links": skipped[:20],
                             "skipped_unsafe_link_count": len(skipped)}
    except Exception as exc:
        report["error"] = {"stage": "extract", "message": f"{type(exc).__name__}: {exc}"[:400]}
        return report

    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/home", "TMPDIR": "/tmp",
           "LANG": "C.UTF-8", "CI": "1", "NO_COLOR": "1"}
    if os.path.exists(ENV_SITE):
        env["PYTHONPATH"] = ENV_SITE

    argv = spec["argv"]
    log, started = _Log(), time.monotonic()
    report["argv"] = argv
    try:
        proc = subprocess.Popen(argv, cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        report["error"] = {"stage": "spawn", "message": str(exc)[:400]}
        return report

    def pump():
        for chunk in iter(lambda: proc.stdout.read(8192), b""):
            log.feed(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()
    reader.join(timeout=5)
    rendered_log = log.render()
    diagnostics = _parse_diagnostics(rendered_log.get("tail", "") + "\n" + rendered_log.get("head", ""))
    report.update({
        "exit": proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": rendered_log,
        "diagnostics": diagnostics,
        "diagnostics_count": len(diagnostics),
    })
    return report


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if text else ""


def _parse_playwright_json(data: dict, selector: str) -> dict:
    """Check the full JSON report before reducing it to bounded response details."""
    stat_keys = ("expected", "unexpected", "skipped", "flaky")
    stats = data["stats"]
    if any(type(stats[key]) is not int or stats[key] < 0 for key in stat_keys):
        raise ValueError("invalid Playwright summary counts")
    root = data["config"]["rootDir"]
    if not os.path.isabs(root):
        raise ValueError("Playwright report rootDir must be absolute")
    suites, raw_errors = data["suites"], data["errors"]
    if not isinstance(suites, list) or not isinstance(raw_errors, list):
        raise ValueError("invalid Playwright suites or errors")
    wanted_path, _, wanted_title = selector.partition("::")
    counts = dict.fromkeys(("collected", "executed", "passed", "failed", "skipped", "flaky",
                            "incomplete", "timed_out", "errors"), 0)
    counts["errors"] = len(raw_errors)
    statuses = collections.Counter()
    specs = set()
    tests = []
    matched = executed = 0

    def visit_suite(suite: dict) -> None:
        nonlocal matched, executed
        for spec in suite.get("specs", []):
            title = spec["title"]
            filename = spec["file"]
            if not isinstance(title, str) or not title or not isinstance(filename, str) or not filename:
                raise ValueError("invalid Playwright spec identity")
            file_path = os.path.relpath(os.path.join(root, filename), WORK)
            selected = file_path == wanted_path and (not wanted_title or title == wanted_title)
            specs.add(file_path)
            project_tests = spec["tests"]
            if not isinstance(project_tests, list) or not project_tests:
                raise ValueError("Playwright spec lacks project results")
            for test in project_tests:
                status = test["status"]
                results = test["results"]
                if status not in stat_keys or not isinstance(results, list):
                    raise ValueError("invalid Playwright test result")
                attempts = [result["status"] for result in results]
                if any(state not in {"passed", "failed", "skipped", "timedOut", "interrupted"} for state in attempts):
                    raise ValueError("invalid Playwright attempt status")
                latest = results[-1] if results else {}
                last = attempts[-1] if attempts else None
                counts["collected"] += 1
                statuses[status] += 1
                if selected:
                    matched += 1
                if last in {"passed", "failed", "timedOut"}:
                    counts["executed"] += 1
                    if selected:
                        executed += 1
                if not results or "interrupted" in attempts:
                    outcome = "incomplete"
                elif last == "timedOut":
                    outcome = "timed_out"
                elif status == "unexpected":
                    outcome = "failed"
                elif status == "flaky":
                    outcome = "flaky"
                elif status == "skipped" and last == "skipped":
                    outcome = "skipped"
                elif status == "expected" and last == "passed":
                    outcome = "passed"
                else:
                    outcome = "incomplete"
                counts[outcome] += 1
                if len(tests) < MAX_TESTS:
                    error = latest.get("error") or next(iter(latest.get("errors") or []), {})
                    tests.append({"id": f"{file_path}::{title}", "title": title, "file": file_path,
                                  "outcome": outcome, "status": last,
                                  "duration_seconds": round(_num(latest.get("duration"), 1e6) / 1000.0, 3),
                                  "error": _strip_ansi(error.get("message", ""))[:500] if error else None})
        for child in suite.get("suites", []):
            visit_suite(child)

    for suite in suites:
        visit_suite(suite)
    if any(statuses[key] != stats[key] for key in stat_keys):
        raise ValueError("Playwright summary disagrees with the project results")

    return {
        "counts": counts, "specs": sorted(specs)[:20], "tests": tests, "tests_total": counts["collected"],
        "selector_coverage": [{"selector": selector, "collected": matched, "executed": executed}],
        "errors": [_strip_ansi(error["message"])[:400] for error in raw_errors[:10]],
    }


def _run_playwright_spec(spec):
    report = {"harness": 1, "kind": "playwright_spec", "profile": spec.get("profile", "playwright-spec"), "extract": {"ok": False}}
    os.makedirs("/tmp/home", exist_ok=True)
    try:
        files, size, skipped = _extract(spec["limits"]["files"], spec["limits"]["bytes"])
        report["extract"] = {"ok": True, "members": files, "bytes": size, "skipped_unsafe_links": skipped[:20],
                             "skipped_unsafe_link_count": len(skipped)}
    except Exception as exc:
        report["error"] = {"stage": "extract", "message": f"{type(exc).__name__}: {exc}"[:400]}
        return report

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp/home",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
        "NODE_PATH": "/usr/local/lib/node_modules",
        "NODE_ENV": "test",
    }
    if os.path.exists(ENV_SITE):
        env["PYTHONPATH"] = ENV_SITE

    # Write .pth into user site packages so even if run_notes_qa.py pops PYTHONPATH,
    # child Python processes (uvicorn, openai_stub) can import from ENV_SITE and WORK
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_site = f"/tmp/home/.local/lib/{py_ver}/site-packages"
    os.makedirs(user_site, exist_ok=True)
    with open(os.path.join(user_site, "odytor_paths.pth"), "w") as pth_file:
        pth_file.write(f"{ENV_SITE}\n{WORK}\n")

    # Link global node_modules into WORK if node_modules is not in WORK
    if not os.path.exists(os.path.join(WORK, "node_modules")) and os.path.isdir("/usr/local/lib/node_modules"):
        try:
            os.symlink("/usr/local/lib/node_modules", os.path.join(WORK, "node_modules"))
        except OSError:
            pass

    pw_cfg = spec.get("playwright_spec") or {}
    launcher_rel = pw_cfg.get("launcher", "tests/e2e/notes-qa/run_notes_qa.py")
    launcher = os.path.join(WORK, launcher_rel)
    if not os.path.isfile(launcher):
        report["error"] = {"stage": "startup", "message": f"Launcher script not found in repository: {launcher_rel}"}
        return report

    # Write Playwright reporter script
    reporter_path = "/tmp/odytor_reporter.js"
    try:
        with open(reporter_path, "w", encoding="utf-8") as rf:
            rf.write(_ODYTOR_REPORTER_JS)
    except OSError:
        pass

    report_dir = "/tmp/playwright-results"
    os.makedirs(report_dir, exist_ok=True)
    report_json = os.path.join(report_dir, "report.json")
    env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = report_json
    env["NOTES_QA_OUTPUT_DIR"] = report_dir

    argv = [sys.executable, launcher]
    parsed_selectors = pw_cfg.get("parsed_selectors") or []
    selectors = spec.get("selectors") or []
    if len(parsed_selectors) != 1 or len(selectors) != 1:
        report["error"] = {"stage": "collection", "message": "Playwright requires exactly one selector."}
        return report
    sel = parsed_selectors[0]
    argv.append(sel["spec_path"])
    title = sel.get("test_title")
    if title:
        escaped_title = re.sub(r"([.*+?^${}()|[\]\\])", r"\\\1", title)
        argv += ["-g", f"(?:^| ){escaped_title}(?: |$)"]
    argv.append(f"--reporter={reporter_path},json")

    log, started = _Log(), time.monotonic()
    report["argv"] = argv
    _emit_progress(type="phase", phase="starting_app", message="Starting application server")
    try:
        proc = subprocess.Popen(argv, cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        report["error"] = {"stage": "spawn", "message": str(exc)[:400]}
        return report

    launched_browser = [False]
    tap = _ProgressTap(log)

    def pump():
        for chunk in iter(lambda: proc.stdout.read1(8192), b""):
            tap.feed(chunk)
            if not launched_browser[0]:
                chunk_lower = chunk.lower()
                if (b"ready at http" in chunk_lower or b"running at http" in chunk_lower or
                        b"scratch server is ready" in chunk_lower or b"application ready" in chunk_lower or
                        b"server ready" in chunk_lower):
                    launched_browser[0] = True
                    _emit_progress(type="phase", phase="launching_browser", message="Application server ready, launching Playwright")
        tap.close()

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(4.0):
            _emit_progress(type="heartbeat", elapsed_seconds=round(time.monotonic() - started, 1))

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()
    stop_heartbeat.set()
    hb.join(timeout=1)
    reader.join(timeout=5)
    _emit_progress(type="phase", phase="finalizing", message="Finalizing test report")

    rendered_log = log.render()
    log_text = rendered_log.get("tail", "") + "\n" + rendered_log.get("head", "")

    # Find server log if created by run_notes_qa.py
    server_log = ""
    for s_log in glob.glob("/tmp/odysseus-notes-qa-*/server.log"):
        try:
            with open(s_log, "r", errors="replace") as f:
                server_log = f.read()[-8192:]
        except OSError:
            pass

    report.update({
        "exit": proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": rendered_log,
        "server_log": server_log if server_log else None,
    })

    # Parse JSON report
    if os.path.isfile(report_json):
        try:
            with open(report_json, "r", encoding="utf-8") as f:
                pw_data = json.load(f)
            report["playwright"] = _parse_playwright_json(pw_data, selectors[0])
        except Exception as exc:
            report["error"] = {"stage": "malformed_report", "message": f"Malformed Playwright report: {exc}"}
    else:
        # Check why report.json was not generated
        if "Scratch server exited during startup" in log_text or "did not become ready" in log_text:
            report["error"] = {"stage": "startup", "message": "Application server failed during startup or readiness wait."}
        elif "No module named" in log_text or "Cannot find module" in log_text:
            report["error"] = {"stage": "dependency", "message": "Missing dependency required for test execution."}
        elif timed_out:
            pass
        else:
            report["error"] = {"stage": "missing_report", "message": f"Playwright launcher exited with status {proc.returncode} without a JSON report."}

    return report


def main(argv):
    with open(os.path.join(INPUT, "job.json"), "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if argv[1:2] == ["prep"]:
        report = _prep(spec)
    elif spec.get("kind") == "node_test":
        report = _run_node(spec)
    elif spec.get("kind") == "playwright_spec":
        report = _run_playwright_spec(spec)
    elif spec.get("kind") in {"linter", "typechecker", "builder", "c_test"}:
        report = _run_cmd(spec)
    else:
        report = _run_tests(spec)
    if isinstance(report, dict) and "artifacts" not in report:
        _attach_log_artifact(report)
    sys.stdout.write("\n" + MARK + json.dumps(report, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
