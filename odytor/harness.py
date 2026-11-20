"""In-sandbox entry point: unpack the source artifact, run the fixed invocation, report.

Standard library only; copied beside the job inputs. The container has no network and no
credentials. Everything this module reports is evidence from inside the sandbox, which
the code under test can influence, and is validated by the host before use.
"""
import collections
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
                if isinstance(item, dict):
                    events.append(item)
                else:
                    malformed += 1
    except OSError:
        pass
    return events, malformed


def _num(value):
    try:
        return float(value)
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
        outcome, message, duration, called = None, "", 0.0, False
        for rep in reports:
            duration += _num(rep.get("d"))
            state, when, xfail = rep.get("o"), rep.get("w"), bool(rep.get("xfail"))
            called = called or when == "call"
            if state == "failed":
                outcome = "failed" if when == "call" and outcome != "error" else "error"
                message = message or str(rep.get("msg", ""))
            elif state == "skipped" and outcome is None:
                outcome = "xfailed" if xfail else "skipped"
                message = str(rep.get("msg", ""))
            elif state == "passed" and when == "call" and outcome is None:
                outcome = "xpassed" if xfail else "passed"
        if outcome == "passed" and not called:
            outcome = None
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


class _Log:
    """Constant-memory log capture: the first and last bytes plus a total."""

    def __init__(self):
        self.head, self.tail, self.total = b"", collections.deque(), 0
        self._tail_len = 0

    def feed(self, chunk):
        self.total += len(chunk)
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
           "PYTHONUNBUFFERED": "1", "ODYTOR_EVENTS": EVENTS}
    log, started = _Log(), time.monotonic()
    report["argv"] = spec["argv"]
    try:
        proc = subprocess.Popen(spec["argv"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
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
    events, malformed = _read_events()
    collected, collect_errors, finish, tests = _summarize(events)
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
    try:
        proc = subprocess.Popen(spec["argv"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        report["error"] = {"stage": "spawn", "message": str(exc)[:400]}
        return report

    def pump():
        written = 0
        with open(TAP_FILE, "wb") as sink:
            for chunk in iter(lambda: proc.stdout.read(8192), b""):
                log.feed(chunk)
                if written < MAX_TAP_BYTES + 1:
                    sink.write(chunk)
                    written += len(chunk)

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


def main(argv):
    with open(os.path.join(INPUT, "job.json"), "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if argv[1:2] == ["prep"]:
        report = _prep(spec)
    elif spec.get("kind") == "node_test":
        report = _run_node(spec)
    elif spec.get("kind") in {"linter", "typechecker", "builder", "c_test"}:
        report = _run_cmd(spec)
    else:
        report = _run_tests(spec)
    sys.stdout.write("\n" + MARK + json.dumps(report, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
