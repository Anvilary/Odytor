"""Pytest plugin that appends raw run events as JSON lines (runs inside the sandbox).

Standard library only. Its output is evidence from the code under test, not a trust root.
"""
import json
import os

_PATH = os.environ.get("ODYTOR_EVENTS")
_MAX_TEXT = 400
_MAX_IDS = 20000


def _emit(**event):
    if not _PATH:
        return
    try:
        with open(_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")
    except Exception:
        pass


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


def pytest_sessionfinish(session, exitstatus):
    _emit(e="finish", exit=int(exitstatus))
