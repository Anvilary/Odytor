"""Pytest plugin that appends raw run events as JSON lines (runs inside the sandbox).

Standard library only. Its output is evidence from the code under test, not a trust root.
"""
import json
import os
import sys

_PATH = os.environ.get("ODYTOR_EVENTS")
_STREAM = os.environ.get("ODYTOR_STREAM_EVENTS")
_MAX_TEXT = 400
_MAX_IDS = 20000


def _emit(**event):
    if _PATH:
        try:
            with open(_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")
        except Exception:
            pass

def _progress(**event):
    if _STREAM:
        try:
            sys.stderr.write("\nODYTOR_PROGRESS:" + json.dumps({"phase": "executing_tests", **event}) + "\n")
            sys.stderr.flush()
        except Exception:
            pass


_ACTIVE = {}


def pytest_runtest_logstart(nodeid):
    if _STREAM:
        _ACTIVE[nodeid] = {"outcome": "passed", "duration_ms": 0}
        _progress(type="test_start", id=nodeid, title=nodeid)


def _text(report):
    try:
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple):
            return str(longrepr[-1])[:_MAX_TEXT]
        crash = getattr(longrepr, "reprcrash", None)
        if crash is not None:
            return str(crash.message)[:_MAX_TEXT]
        return str(longrepr)[-_MAX_TEXT:] if longrepr is not None else ""
    except Exception:
        return ""


def pytest_collection_finish(session):
    ids = [item.nodeid for item in session.items]
    _emit(e="collected", n=len(ids), ids=ids[:_MAX_IDS])
    _progress(type="collection", total=len(ids))


def pytest_collectreport(report):
    if report.failed:
        _emit(e="collect_error", id=report.nodeid, msg=_text(report))


def pytest_runtest_logreport(report):
    event = {"e": "report", "id": report.nodeid, "w": report.when, "o": report.outcome,
             "d": round(float(report.duration), 4)}
    if hasattr(report, "wasxfail"):
        event["xfail"] = True
    if report.outcome != "passed" or event.get("xfail"):
        event["msg"] = _text(report)
    _emit(**event)
    live = _ACTIVE.get(report.nodeid)
    if live is not None:
        live["duration_ms"] += int(report.duration * 1000)
        if report.failed:
            if live["outcome"] != "error":
                live["outcome"] = "failed" if report.when == "call" else "error"
        elif report.skipped and live["outcome"] == "passed":
            live["outcome"] = "skipped"
        if report.when == "teardown":
            _ACTIVE.pop(report.nodeid)
            _progress(type="test_end", id=report.nodeid, title=report.nodeid, **live)


def pytest_sessionfinish(session, exitstatus):
    _emit(e="finish", exit=int(exitstatus))
