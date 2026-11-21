"""Playwright's JSON reporter nests project tests under specs."""
import pytest

from odytor.harness import _parse_playwright_json


def report(result, *, file="a/example.spec.ts", status="expected"):
    return {
        "config": {"rootDir": "/work/tests/e2e"},
        "suites": [{"suites": [{"specs": [{"title": "saves a note", "file": file, "tests": [
            {"status": status, "results": result},
        ]}]}]}],
        "errors": [],
        "stats": {key: int(key == status) for key in ("expected", "unexpected", "flaky", "skipped")},
    }


def test_report_coverage_preserves_full_path_and_literal_title():
    selector = "tests/e2e/a/example.spec.ts::saves a note"
    matching = _parse_playwright_json(report([{"status": "passed"}]), selector)
    assert matching["counts"]["passed"] == 1
    assert matching["selector_coverage"] == [{"selector": selector, "collected": 1, "executed": 1}]
    same_basename = _parse_playwright_json(report([{"status": "passed"}], file="b/example.spec.ts"), selector)
    assert same_basename["selector_coverage"] == [{"selector": selector, "collected": 0, "executed": 0}]
    longer_title = _parse_playwright_json(report([{"status": "passed"}]), selector + " twice")
    assert longer_title["selector_coverage"][0]["collected"] == 0


@pytest.mark.parametrize("results", [[], [{"status": "interrupted"}]])
def test_missing_or_interrupted_attempts_are_incomplete(results):
    out = _parse_playwright_json(report(results, status="skipped"), "tests/e2e/a/example.spec.ts")
    assert out["counts"]["incomplete"] == 1
    assert out["counts"]["passed"] == 0


def test_malformed_summary_is_rejected():
    data = report([{"status": "passed"}])
    data["stats"]["expected"] = 2
    with pytest.raises(ValueError, match="disagrees"):
        _parse_playwright_json(data, "tests/e2e/a/example.spec.ts")
