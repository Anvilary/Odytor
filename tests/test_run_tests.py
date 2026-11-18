"""test_run / test_result: validation, exact revisions, authorization and result evidence."""
import asyncio
import contextlib
import io
import subprocess
import tarfile
import time

import pytest

from git_plus.config import RunSettings, Settings
from git_plus.errors import GitPlusError, RevisionMismatchError, SecurityError, ValidationError
from git_plus.git_workspace import GitWorkspace
from git_plus.jobs import JobService, QueueFullError
from git_plus.profile import load_policies
from git_plus.run_tests import fetch_test_result, submit_test_run
from git_plus.server import create_server
from test_git_workspace import client_for, local_fetch, repository, run
from test_jobs import payload, wait_for

REPO = 'owner/repo'


@pytest.fixture
def stack(tmp_path, local_fetch):
    """A real Git repository with tests, a policy, a fake isolated runner and a real job service."""
    repo, _ = local_fetch
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    (repo / '.gitattributes').write_text('tests/secret_helper.py export-ignore\n')
    (repo / 'tests/secret_helper.py').write_text('HELPER = 1\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'tests')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    (tmp_path / 'req.txt').write_text('pytest==8.3.5\n')
    (tmp_path / 'policy.toml').write_text(f'[repos."{REPO}"]\nrequirements = "req.txt"\ntest_dirs = ["tests"]\n')
    policies = load_policies(tmp_path / 'policy.toml')
    cfg = RunSettings(runner_command=('true',), policies_path=str(tmp_path / 'policy.toml'), data_dir=str(tmp_path / 'data'),
                      max_queued=2, min_free_bytes=1, max_source_bytes=50 * 1024 * 1024)
    jobs = []

    def runner(job, cancel):
        jobs.append(job)
        job.captured_tar = (job.spool / 'source.tar').read_bytes()
        coverage = [{'selector': sel, 'collected': 2, 'executed': 2} for sel in job.header['selectors']]
        options = {'coverage': coverage, **runner.options}
        return payload(spec=policies[REPO].requirements_sha256, **options)
    runner.options = {}
    service = JobService(cfg, runner=runner)
    yield type('Stack', (), dict(repo=repo, head=head, policies=policies, cfg=cfg, service=service, jobs=jobs,
                                 runner=runner, client=client_for(head)))
    service.shutdown()


def submit(stack, client=None, expected_head=None, **kw):
    return submit_test_run(client or stack.client, REPO, 1, expected_head or stack.head, service=stack.service,
                           policies=stack.policies, owner='alice', cfg=stack.cfg, **kw)


def spool_files(stack):
    return list(stack.service.spool_root.iterdir()) if stack.service.spool_root.exists() else []


def tar_files(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}


def test_valid_head_run_queues_executes_and_reports_exact_tree(stack):
    queued = submit(stack, selectors=['tests/test_a.py::test_ok'])
    assert queued['status'] == 'queued' and queued['requested_head'] == stack.head
    assert queued['verified']['head_sha'] == stack.head and queued['mode'] == 'head'
    assert queued['trees']['effective_tree'] == queued['trees']['pr_tree'] == run(stack.repo, 'rev-parse', 'HEAD^{tree}').decode().strip()
    assert queued['tests_ran_against'] == 'unmodified_pr_tree' and queued['github_mutations'] == 'none'
    done = wait_for(stack.service, queued['run_id'], {'passed'})
    assert done['result']['environment']['cache_status'] == 'hit'
    job = stack.jobs[0]
    assert job.header['selectors'] == ['tests/test_a.py::test_ok'] and 'token' not in str(job.header).lower().replace('tokens', '')
    # The artifact is the exact tree, including a file .gitattributes would drop from `git archive`.
    files = tar_files(job.captured_tar)
    assert 'tests/secret_helper.py' in files and files['tests/test_a.py'].startswith(b'def test_ok')
    assert not job.spool.exists()
    client = stack.client
    client.graphql.assert_not_called()
    assert all(call.args[0] == 'GET' for call in client.request.call_args_list)


def test_default_selectors_are_the_policy_test_dirs(stack):
    assert submit(stack)['selectors'] == ['tests']


@pytest.mark.parametrize('kwargs,error', [
    (dict(expected_head='zz'), ValidationError), (dict(expected_head='b' * 40), RevisionMismatchError),
    (dict(profile='shell'), ValidationError), (dict(mode='merge'), ValidationError),
    (dict(selectors=['../x.py']), ValidationError), (dict(selectors=['-x']), ValidationError),
    (dict(selectors=['/etc/passwd']), ValidationError), (dict(selectors=['src/a.py']), ValidationError),
    (dict(selectors=[]), ValidationError), (dict(expected_base='a' * 40), ValidationError),
    (dict(mode='prospective_merge'), ValidationError), (dict(mode='prospective_merge', expected_base='nope'), ValidationError),
    (dict(test_overlay='x'), ValidationError)])
def test_invalid_requests_are_rejected_before_any_job_exists(stack, kwargs, error):
    with pytest.raises(error):
        submit(stack, **kwargs)
    assert not stack.service.store.records() and not spool_files(stack)
    assert stack.service._reserved == 0


def test_repository_without_policy_and_bad_repository_are_rejected(stack):
    with pytest.raises(SecurityError):
        submit_test_run(stack.client, 'other/repo', 1, stack.head, service=stack.service, policies=stack.policies, owner='a', cfg=stack.cfg)
    with pytest.raises(ValidationError):
        submit_test_run(stack.client, 'not a repo', 1, stack.head, service=stack.service, policies=stack.policies, owner='a', cfg=stack.cfg)


def test_user_without_repository_access_is_rejected(stack):
    stack.client.request.return_value.json.return_value = {'permissions': {'pull': False, 'push': False}}
    with pytest.raises(SecurityError):
        submit(stack)
    assert not stack.service.store.records()


def test_closed_or_merged_prs_cannot_be_tested(stack):
    stack.client.get_pr.return_value['state'] = 'closed'
    with pytest.raises(ValidationError):
        submit(stack)


def test_queue_saturation_rejects_and_releases_reservation(stack):
    import threading
    gate = threading.Event()
    original = stack.runner
    stack.service.runner = lambda job, cancel: (gate.wait(10), original(job, cancel))[1]
    try:
        running = submit(stack)
        wait_for(stack.service, running['run_id'], {'running'})
        submit(stack); submit(stack)
        with pytest.raises(QueueFullError):
            submit(stack)
        assert len(list(stack.service.spool_root.iterdir())) == 3
    finally:
        gate.set()


def test_stale_head_branch_advancing_during_preparation_fails_safely(stack, monkeypatch):
    def advanced(self, target, ref, expected, label, **kw):
        raise RevisionMismatchError(expected, 'c' * 40, revision=label)
    monkeypatch.setattr(GitWorkspace, 'fetch', advanced)
    with pytest.raises(RevisionMismatchError):
        submit(stack)
    assert not stack.service.store.records() and not spool_files(stack)


def test_pr_head_moving_after_preparation_is_not_silently_accepted(stack):
    original = stack.client.get_pr.return_value
    calls = {'n': 0}

    def moving(*a, **k):
        calls['n'] += 1
        result = dict(original)
        if calls['n'] >= 2:
            result = {**original, 'head': {**original['head'], 'sha': 'd' * 40}}
        return result
    stack.client.get_pr.side_effect = moving
    with pytest.raises(RevisionMismatchError):
        submit(stack)
    assert not stack.service.store.records()


# --- prospective merge -----------------------------------------------------------------------
def diverge(stack, conflict):
    repo = stack.repo
    run(repo, 'checkout', '-qb', 'main', 'HEAD')
    (repo / ('tests/test_a.py' if conflict else 'base_only.py')).write_text('def test_ok():\n    assert 1 == 1  # base\n' if conflict else 'BASE = 1\n')
    run(repo, 'add', '.'); run(repo, 'commit', '-qm', 'base change')
    base = run(repo, 'rev-parse', 'HEAD').decode().strip()
    run(repo, 'checkout', '-q', 'feature')
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert 2 == 2  # head\n')
    run(repo, 'commit', '-qam', 'head change')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    stack.client = client_for(head)
    stack.client.get_pr.return_value['base']['sha'] = 'e' * 40  # stale API snapshot must be irrelevant
    return base, head


def test_clean_prospective_merge_tests_the_exact_merge_tree(stack):
    base, head = diverge(stack, conflict=False)
    queued = submit(stack, expected_head=head, mode='prospective_merge', expected_base=base)
    merged = run(stack.repo, 'merge-tree', '--write-tree', base, head).decode().strip()
    assert queued['trees']['merge_tree'] == queued['trees']['effective_tree'] == merged
    assert queued['verified']['base_sha'] == base and queued['verified']['base_sha_role'] == 'live_target_branch_tip'
    wait_for(stack.service, queued['run_id'], {'passed'})
    files = tar_files(stack.jobs[0].captured_tar)
    assert files['base_only.py'] == b'BASE = 1\n' and b'# head' in files['tests/test_a.py']


def test_conflicting_merge_reports_conflict_and_never_runs(stack):
    base, head = diverge(stack, conflict=True)
    result = submit(stack, expected_head=head, mode='prospective_merge', expected_base=base)
    assert result['status'] == 'merge_conflict' and result['conflict']['paths'] == ['tests/test_a.py']
    assert result['green'] is False and 'effective_tree' not in result['trees']
    time.sleep(0.2)
    assert not stack.jobs and not spool_files(stack) and stack.service._reserved == 0
    assert stack.service.store.load(result['run_id'])['status'] == 'merge_conflict'


def test_stale_expected_base_is_rejected_even_if_it_matches_the_pr_api_snapshot(stack):
    base, head = diverge(stack, conflict=False)
    older = run(stack.repo, 'rev-parse', f'{base}~1').decode().strip()
    stack.client.get_pr.return_value['base']['sha'] = older
    with pytest.raises(RevisionMismatchError) as caught:
        submit(stack, expected_head=head, mode='prospective_merge', expected_base=older)
    assert caught.value.diagnostic['revision'] == 'base'
    assert not stack.service.store.records()


# --- results and authorization -----------------------------------------------------------------
def test_result_is_bound_to_the_submitting_principal(stack):
    queued = submit(stack)
    wait_for(stack.service, queued['run_id'], {'passed'})
    view = fetch_test_result(stack.client, REPO, queued['run_id'], service=stack.service, owner='alice')
    assert view['status'] == 'passed' and view['green'] is True and view['currency']['tested_head_is_current_pr_head']
    for intruder in ('bob', ''):
        with pytest.raises(ValidationError, match='Unknown run_id'):
            fetch_test_result(stack.client, REPO, queued['run_id'], service=stack.service, owner=intruder)
    with pytest.raises(ValidationError, match='Unknown run_id'):
        fetch_test_result(stack.client, REPO, 'test-' + '0' * 32, service=stack.service, owner='alice')
    with pytest.raises(ValidationError):
        fetch_test_result(stack.client, 'other/repo', queued['run_id'], service=stack.service, owner='alice')


def test_result_withheld_when_repository_access_cannot_be_verified(stack):
    from git_plus.errors import APIError
    queued = submit(stack)
    wait_for(stack.service, queued['run_id'], {'passed'})
    stack.client.get_pr.side_effect = APIError('forbidden', 403)
    with pytest.raises(SecurityError):
        fetch_test_result(stack.client, REPO, queued['run_id'], service=stack.service, owner='alice')


def test_historical_result_reports_when_it_is_no_longer_current(stack):
    queued = submit(stack)
    wait_for(stack.service, queued['run_id'], {'passed'})
    stack.client.get_pr.return_value['head']['sha'] = 'f' * 40
    view = fetch_test_result(stack.client, REPO, queued['run_id'], service=stack.service, owner='alice')
    assert view['status'] == 'passed' and view['currency']['tested_head_is_current_pr_head'] is False
    assert view['verified']['head_sha'] == stack.head


def test_pr_closure_shortens_retention_of_that_prs_results_only(stack):
    first, other = submit(stack), submit(stack)
    wait_for(stack.service, first['run_id'], {'passed'}); wait_for(stack.service, other['run_id'], {'passed'})
    stack.service.mutate(other['run_id'], lambda r: r.__setitem__('pr', 99))
    stack.client.get_pr.return_value['state'] = 'closed'
    before = stack.service.store.load(first['run_id'])['expires_at']
    fetch_test_result(stack.client, REPO, first['run_id'], service=stack.service, owner='alice')
    after = stack.service.store.load(first['run_id'])['expires_at']
    assert after < before and after <= time.time() + stack.cfg.closed_grace_hours * 3600 + 5
    assert stack.service.store.load(other['run_id'])['expires_at'] == before or stack.service.store.load(other['run_id'])['expires_at'] > after


def test_non_green_results_are_never_green(stack):
    stack.runner.options = {'exit': 5, 'finished': 5, 'passed': 0, 'collected': 0}
    queued = submit(stack)
    view = fetch_test_result(stack.client, REPO, queued['run_id'], service=stack.service, owner='alice') if wait_for(
        stack.service, queued['run_id'], {'no_tests_collected'}) else None
    assert view['green'] is False and view['failure']['kind'] == 'no_tests_collected'


# --- MCP layer -------------------------------------------------------------------------------
def make_server(stack, monkeypatch, *, principal='alice', allowed=()):
    import git_plus.server as server_module
    stack.client.principal = principal

    @contextlib.contextmanager
    def fake_client(cfg, remote=False, *, repo=None):
        yield stack.client
    monkeypatch.setattr(server_module, 'github_client', fake_client)
    monkeypatch.setattr(server_module, 'JobService', lambda cfg: stack.service)
    settings = Settings(github_token='t', github_api_url='https://api.github.com', auth_token=None, default_max_diff_lines=500,
                        default_max_diff_bytes=1048576, allowed_repositories=allowed, testing=stack.cfg)
    return create_server(settings)


def tool(server, name):
    return asyncio.run(server.get_tool(name))


def test_mcp_tools_schema_and_end_to_end_with_owner_binding(stack, monkeypatch):
    server = make_server(stack, monkeypatch)
    run_tool, result_tool = tool(server, 'test_run'), tool(server, 'test_result')
    assert set(run_tool.parameters['required']) == {'repo', 'number', 'expected_head'}
    assert result_tool.parameters['required'] == ['run_id']
    assert set(run_tool.parameters['properties']) >= {'profile', 'selectors', 'mode', 'expected_base', 'test_overlay'}
    submitted = tool(server, 'test_run').fn(repo=REPO, number=1, expected_head=stack.head)
    assert submitted['status'] == 'queued' and submitted['run_id'].startswith('test-')
    wait_for(stack.service, submitted['run_id'], {'passed'})
    assert tool(server, 'test_result').fn(run_id=submitted['run_id'])['status'] == 'passed'
    stack.client.principal = 'mallory'
    denied = tool(server, 'test_result').fn(run_id=submitted['run_id'])
    assert denied['error'] == 'Unknown run_id.'
    assert tool(server, 'test_result').fn(run_id='test-' + '1' * 32)['error'] == 'Unknown run_id.'
    assert tool(server, 'test_result').fn(run_id='../etc/passwd')['error'] == 'Unknown run_id.'


def test_mcp_enforces_allowlist_and_unconfigured_state(stack, monkeypatch):
    server = make_server(stack, monkeypatch, allowed=('someone/else',))
    with pytest.raises(SecurityError):
        tool(server, 'test_run').fn(repo=REPO, number=1, expected_head=stack.head)
    plain = create_server(Settings(github_token='t', github_api_url='https://api.github.com', auth_token=None,
                                   default_max_diff_lines=500, default_max_diff_bytes=1048576, allowed_repositories=()))
    assert 'not configured' in tool(plain, 'test_run').fn(repo=REPO, number=1, expected_head='a' * 40)['error']
    assert 'not configured' in tool(plain, 'test_result').fn(run_id='test-' + '1' * 32)['error']
