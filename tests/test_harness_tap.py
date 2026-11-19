"""The in-sandbox TAP parser, exercised on real Node 22 output shapes (no sandbox needed)."""
import os

import pytest

from odytor import harness

SUMMARY = "1..{n}\n# tests {tests}\n# suites {suites}\n# pass {passed}\n# fail {fail}\n# cancelled 0\n# skipped {skipped}\n# todo 0\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("empty.test.mjs", "real.test.mjs", "broken.test.mjs"):
        (tmp_path / "tests" / name).write_text("//\n")
    return tmp_path


def parse(tree, text):
    path = tree / "out.tap"
    path.write_text(text)
    return harness._parse_tap(str(path), root=str(tree))


def test_a_file_that_defines_no_tests_is_vacuous_not_a_pass(tree):
    tap = parse(tree, "TAP version 13\n# Subtest: tests/empty.test.mjs\nok 1 - tests/empty.test.mjs\n" + SUMMARY.format(n=1, tests=1, suites=0, passed=1, fail=0, skipped=0))
    assert tap["vacuous_files"] == ["tests/empty.test.mjs"] and tap["tests"] == []
    assert tap["counts"]["tests"] == 0 and tap["counts"]["pass"] == 0 and tap["summary_found"]


def test_real_tests_and_suites_are_counted_and_a_vacuous_neighbour_is_still_removed(tree):
    text = ("TAP version 13\n# Subtest: suite\n    # Subtest: one\n    ok 1 - one\n    # Subtest: two\n    ok 2 - two\n    1..2\nok 1 - suite\n"
            "# Subtest: tests/empty.test.mjs\nok 2 - tests/empty.test.mjs\n" + SUMMARY.format(n=2, tests=3, suites=1, passed=3, fail=0, skipped=0))
    tap = parse(tree, text)
    assert [t["id"] for t in tap["tests"]] == ["one", "two", "suite"] and tap["vacuous_files"] == ["tests/empty.test.mjs"]
    assert tap["counts"]["tests"] == 2 and tap["counts"]["pass"] == 2


def test_a_file_level_failure_stays_a_failure(tree):
    text = ("TAP version 13\n# Subtest: tests/broken.test.mjs\nnot ok 1 - tests/broken.test.mjs\n  ---\n  error: 'SyntaxError: bad'\n  ...\n"
            + SUMMARY.format(n=1, tests=1, suites=0, passed=0, fail=1, skipped=0))
    tap = parse(tree, text)
    assert tap["vacuous_files"] == [] and tap["counts"]["fail"] == 1
    assert tap["tests"][0]["outcome"] == "failed" and "SyntaxError" in tap["tests"][0]["message"]


def test_a_test_named_like_a_path_that_does_not_exist_is_a_real_test(tree):
    tap = parse(tree, "TAP version 13\nok 1 - tests/not-a-file.test.mjs\n" + SUMMARY.format(n=1, tests=1, suites=0, passed=1, fail=0, skipped=0))
    assert tap["vacuous_files"] == [] and tap["counts"]["pass"] == 1


def test_absolute_and_parent_names_never_probe_the_filesystem(tree):
    tap = parse(tree, "TAP version 13\nok 1 - /etc/passwd\nok 2 - ../out.tap\n" + SUMMARY.format(n=2, tests=2, suites=0, passed=2, fail=0, skipped=0))
    assert tap["vacuous_files"] == [] and len(tap["tests"]) == 2


def test_incomplete_or_missing_summary_is_reported():
    assert harness._parse_tap("/nonexistent/path")["summary_found"] is False
