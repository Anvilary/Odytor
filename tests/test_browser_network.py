"""Unit tests for deterministic browser network request counting, reset scopes, and assertions."""
import pytest
from odytor.browser_driver import Run, count_requests, execute_step, summarize_network


def test_network_request_counting_and_reset_scopes():
    run = Run({}, {"mocks": []}, 8000)
    # Simulate step 1: 1 request to /api/hwfit/models
    run.network.append({"method": "GET", "path": "/api/hwfit/models", "type": "fetch", "mocked": False, "blocked": False})
    
    # Assert requests before reset
    assert count_requests(run.network, "/api/hwfit/models", "GET", run.baseline) == 1
    
    # Simulate reset_network_counts
    res_reset = execute_step(run, None, {"op": "reset_network_counts"}, 1, None)
    assert res_reset["reset"] is True
    assert run.baseline == 1
    
    # Assert requests right after reset (expecting 0)
    assert count_requests(run.network, "/api/hwfit/models", "GET", run.baseline) == 0
    res_assert_0 = execute_step(run, None, {"op": "assert_requests", "path": "/api/hwfit/models", "count": 0, "method": "GET"}, 2, None)
    assert res_assert_0["requests"] == 0
    assert res_assert_0["since_last_reset"] is True

    # Simulate an unrelated request
    run.network.append({"method": "GET", "path": "/api/other", "type": "fetch", "mocked": False, "blocked": False})
    assert count_requests(run.network, "/api/hwfit/models", "GET", run.baseline) == 0

    # Simulate step 3: 1 new request to /api/hwfit/models
    run.network.append({"method": "GET", "path": "/api/hwfit/models", "type": "fetch", "mocked": False, "blocked": False})
    
    # Assert requests since reset: should be 1
    assert count_requests(run.network, "/api/hwfit/models", "GET", run.baseline) == 1
    res_assert_1 = execute_step(run, None, {"op": "assert_requests", "path": "/api/hwfit/models", "count": 1, "method": "GET"}, 4, None)
    assert res_assert_1["requests"] == 1

    # Check network summary: total is 3, since_last_reset is 2, replayed_total is 0
    summary = summarize_network(run.network, baseline=run.baseline, replayed_total=run.replayed_total)
    assert summary["total"] == 3
    assert summary["since_last_reset"] == 2
    assert summary["replayed_total"] == 0
    assert summary["by_path"]["GET /api/hwfit/models"] == 2
    assert summary["by_path_since_reset"]["GET /api/hwfit/models"] == 1
    assert "scope_explanation" in summary


def test_assert_requests_negative_control_failure():
    run = Run({}, {"mocks": []}, 8000)
    run.network.append({"method": "GET", "path": "/api/models", "type": "fetch", "mocked": False, "blocked": False})
    
    # Expecting 0 when 1 exists should raise AssertionError with explanatory text
    with pytest.raises(AssertionError) as exc_info:
        execute_step(run, None, {"op": "assert_requests", "path": "/api/models", "count": 0, "method": "GET"}, 1, None)
    assert "expected eq 0 request(s) to /api/models since the last reset" in str(exc_info.value)
    assert "observed 1" in str(exc_info.value)
    assert "baseline index 0" in str(exc_info.value)


def test_network_summary_with_replay():
    run = Run({}, {"mocks": []}, 8000)
    # 2 requests occurred during initial session
    initial_entries = [
        {"method": "GET", "path": "/index.html", "type": "document", "mocked": False, "blocked": False},
        {"method": "GET", "path": "/app.js", "type": "script", "mocked": False, "blocked": False},
    ]
    # In continuation replay, these are recorded into replayed_total
    replayed_count = len(initial_entries)
    
    # New steps in continuation turn issue 1 request
    new_entries = [
        {"method": "GET", "path": "/api/items", "type": "fetch", "mocked": False, "blocked": False},
    ]
    summary = summarize_network(new_entries, baseline=0, replayed_total=replayed_count)
    assert summary["total"] == 1
    assert summary["replayed_total"] == 2
    assert summary["since_last_reset"] == 1
