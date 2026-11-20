"""Guarded publication: plan is read-only, apply is bound to exact inputs, uncertain outcomes are reconciled."""
import pytest

from odytor.config import WorkspaceSettings
from odytor.errors import APIError, GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from odytor.publish import PlanStore, apply_publication, plan_publication
from odytor.workspace import VersionConflictError, WorkspaceRepo, WorkspaceService, open_workspace
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

OWNER = 'alice'
LIMITS = dict(max_files=10, max_changed_lines=1000, max_patch_bytes=262144)


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def stack(tmp_path, clock, local_fetch):
    repo, sha = local_fetch
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'tests')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    service = WorkspaceService(WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024), clock=clock)
    client = client_for(head)
    client.graphql.reset_mock()
    plans = PlanStore(tmp_path / 'plans', clock=clock)

    def open_ws(operations=('read', 'edit', 'diff', 'execute', 'publish'), **kw):
        return open_workspace(client, 'owner/repo', service=service, owner=OWNER, number=1, expected_head=head, operations=list(operations), **kw)
    return service, plans, client, repo, head, open_ws


def plan(stack, w, version, message='test: cover the empty case', **kw):
    service, plans, client, *_ = stack
    return plan_publication(client, 'owner/repo', w, version, message, workspaces=service, plans=plans, jobs=None, owner=OWNER, **{**LIMITS, **kw})


def apply(stack, plan_id, confirm, authorized=True, owner=OWNER, approval_token=None):
    service, plans, client, *_ = stack
    return apply_publication(client, 'owner/repo', plan_id, confirm, authorized, workspaces=service, plans=plans, jobs=None, owner=owner, approval_token=approval_token)


def commit_response(tree, head, sha='f' * 40):
    return {'createCommitOnBranch': {'commit': {'oid': sha, 'url': f'https://github.com/owner/repo/commit/{sha}',
                                                'tree': {'oid': tree}, 'parents': {'totalCount': 1, 'nodes': [{'oid': head}]}}}}


def edited(stack, ws_summary=None):
    service, plans, client, repo, head, open_ws = stack
    w = (ws_summary or open_ws())['workspace_id']
    service.write_file(w, OWNER, expected_version=0, path='tests/test_b.py', content='def test_b():\n    assert 1 + 1 == 2\n')
    service.replace_text(w, OWNER, expected_version=1, path='tests/test_a.py', old='assert True', new='assert 1')
    return w, 2


# --- planning ----------------------------------------------------------------------------------------------------

def test_a_plan_binds_exact_inputs_and_writes_nothing(stack):
    service, plans, client, repo, head, open_ws = stack
    w, v = edited(stack)
    result = plan(stack, w, v)
    record = service.get(w, OWNER, operation='read')
    assert result['state'] == 'pending' and result['workspace_version'] == 2 and result['tree'] == record['tree'] and result['base_tree'] == record['source_tree']
    assert result['target']['head_sha'] == head and result['target']['number'] == 1 and result['target']['is_fork'] is False
    assert {(f['path'], f['status']) for f in result['files']} == {('tests/test_a.py', 'modified'), ('tests/test_b.py', 'added')}
    assert result['patch_sha256'] == service.diff(w, OWNER)['patch_sha256'] and 'diff --git a/tests/test_b.py' in result['patch']
    assert result['github_mutations'].startswith('none') and 'user_authorized' in result['required_authorization']
    assert result['confirmation'] == f"publish {result['plan_id']} tree {record['tree'][:12]} patch {result['patch_sha256'][:12]}"
    assert any('No test run exists for this exact tree' in l for l in result['limitations'])
    client.graphql.assert_not_called()
    assert not any(c.args[:1] == ('POST',) or c.args[:1] == ('PUT',) for c in client.request.call_args_list)


@pytest.mark.parametrize('path,content', [('.github/workflows/ci.yml', 'on: push\n'), ('requirements.txt', 'evil==1\n'), ('scripts/run.sh', 'rm -rf /\n'),
                                          ('src/auth_token.py', 'x = 1\n'), ('Dockerfile', 'FROM x\n'), ('deploy/prod.yaml', 'a: 1\n')])
def test_paths_the_quick_fix_policy_forbids_cannot_be_published(stack, path, content):
    service, plans, client, repo, head, open_ws = stack
    w = open_ws()['workspace_id']
    service.write_file(w, OWNER, expected_version=0, path=path, content=content)
    with pytest.raises(SecurityError) as info:
        plan(stack, w, 1)
    assert info.value.diagnostic['stage'] == 'path_policy' and info.value.diagnostic['violations'][0]['path'] == path
    assert plans.root.exists() is False or not list(plans.root.glob('pp-*.json'))


def test_plan_preconditions(stack):
    service, plans, client, repo, head, open_ws = stack
    w = open_ws()['workspace_id']
    with pytest.raises(ValidationError, match='nothing to publish'):
        plan(stack, w, 0)
    service.write_file(w, OWNER, expected_version=0, path='tests/x.py', content='x = 1\n')
    with pytest.raises(VersionConflictError):
        plan(stack, w, 0)
    for bad in ('', '   ', 'x' * 1441, 'a\x00b'):
        with pytest.raises(ValidationError):
            plan(stack, w, 1, message=bad)
    with pytest.raises(ValidationError, match='1 files|touches 1 files'):
        plan(stack, w, 1, max_files=0)
    # the workspace scope must name publish explicitly: the default scope cannot publish
    default = open_ws(operations=('read', 'edit', 'diff', 'execute'))['workspace_id']
    service.write_file(default, OWNER, expected_version=0, path='tests/x.py', content='x = 1\n')
    with pytest.raises(SecurityError, match="does not include 'publish'"):
        plan(stack, default, 1)


def test_merge_and_branch_workspaces_cannot_be_published(stack):
    service, plans, client, repo, head, open_ws = stack
    branch = open_workspace(client, 'owner/repo', service=service, owner=OWNER, source='branch', branch='feature', expected_sha=head,
                            operations=['read', 'edit', 'diff', 'publish'])['workspace_id']
    service.write_file(branch, OWNER, expected_version=0, path='tests/x.py', content='x = 1\n')
    with pytest.raises(ValidationError, match='PR head'):
        plan(stack, branch, 1)


def test_planning_notices_a_moved_head_or_a_closed_pr(stack):
    service, plans, client, repo, head, open_ws = stack
    w, v = edited(stack)
    client.get_pr.return_value['head']['sha'] = 'a' * 40
    with pytest.raises(RevisionMismatchError):
        plan(stack, w, v)
    client.get_pr.return_value['head']['sha'] = head
    client.get_pr.return_value['state'] = 'closed'
    with pytest.raises(ValidationError, match='no longer open'):
        plan(stack, w, v)


def test_plan_reports_runs_on_this_exact_tree_only(stack):
    service, plans, client, repo, head, open_ws = stack
    w, v = edited(stack)
    tree = service.get(w, OWNER, operation='read')['tree']

    class Jobs:
        class store:
            @staticmethod
            def load(run_id):
                return {'test-' + 'a' * 32: {'profile': 'python-pytest', 'kind': 'workspace', 'status': 'passed', 'trees': {'effective_tree': tree}, 'revisions': {'workspace_version': 2}},
                        'test-' + 'b' * 32: {'profile': 'python-pytest', 'kind': 'workspace', 'status': 'passed', 'trees': {'effective_tree': 'f' * 40}, 'revisions': {'workspace_version': 1}}}.get(run_id)
    service.attach_run(w, 'test-' + 'a' * 32)
    service.attach_run(w, 'test-' + 'b' * 32)
    out = plan_publication(client, 'owner/repo', w, v, 'msg', workspaces=service, plans=plans, jobs=Jobs, owner=OWNER, **LIMITS)
    assert [r['run_id'] for r in out['validation']['runs_on_this_tree']] == ['test-' + 'a' * 32] and out['validation']['runs_on_other_trees'] == 1
    assert out['validation']['any_green_on_this_tree'] is True and not any('No test run' in l for l in out['limitations'])
    assert out['validation']['runs_not_green'] == 0 and not any('did not pass' in l for l in out['limitations'])


def test_a_passing_run_does_not_hide_failed_runs_on_the_same_tree(stack):
    service, plans, client, repo, head, open_ws = stack
    w, v = edited(stack)
    tree = service.get(w, OWNER, operation='read')['tree']
    runs = {'test-' + 'a' * 32: 'passed', 'test-' + 'b' * 32: 'failed', 'test-' + 'c' * 32: 'timeout', 'test-' + 'd' * 32: 'completed'}

    class Jobs:
        class store:
            @staticmethod
            def load(run_id):
                return {'profile': 'browser-playwright', 'kind': 'browser', 'status': runs[run_id], 'trees': {'effective_tree': tree}, 'revisions': {'workspace_version': 2}}
    for run_id in runs:
        service.attach_run(w, run_id)
    out = plan_publication(client, 'owner/repo', w, v, 'msg', workspaces=service, plans=plans, jobs=Jobs, owner=OWNER, **LIMITS)
    assert out['validation']['any_green_on_this_tree'] is True and out['validation']['all_finished_runs_green'] is False
    assert out['validation']['runs_not_green'] == 2  # failed and timeout; an exploration run is neither
    assert any('2 run(s) on this exact tree did not pass' in l for l in out['limitations'])


# --- applying ------------------------------------------------------------------------------------------------------

def approved(stack):
    w, v = edited(stack)
    p = plan(stack, w, v)
    return w, p


def test_apply_requires_explicit_authorization_and_the_exact_confirmation(stack):
    service, plans, client, *_ = stack
    w, p = approved(stack)
    with pytest.raises(SecurityError, match='explicit user approval') as info:
        apply(stack, p['plan_id'], p['confirmation'], authorized=False)
    from odytor.diagnostics import safe_payload
    assert 'REDACTED' not in safe_payload(info.value.to_dict())['error']  # the refusal must stay readable after payload redaction
    with pytest.raises(SecurityError, match='explicit user approval'):
        apply(stack, p['plan_id'], p['confirmation'], authorized='true')
    for wrong in ('', 'yes', p['confirmation'] + ' ', p['confirmation'].replace('tree', 'TREE'), 'publish pp-x tree a patch b'):
        with pytest.raises(SecurityError, match='confirmation does not match'):
            apply(stack, p['plan_id'], wrong)
    client.graphql.assert_not_called()
    assert plans.get(p['plan_id'], OWNER)['state'] == 'pending'


def test_apply_writes_one_additive_commit_verifies_it_and_is_idempotent(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.graphql.return_value = commit_response(p['tree'], head)
    done = apply(stack, p['plan_id'], p['confirmation'])
    assert done['state'] == 'applied' and done['result']['new_sha'] == 'f' * 40 and done['result']['tree_sha'] == p['tree']
    assert done['result']['force_push'] is False and done['result']['history_rewritten'] is False and done['result']['remote_verified'] is True
    [call] = client.graphql.call_args_list
    sent = call.args[1]['input']
    assert sent['expectedHeadOid'] == head and sent['branch'] == {'repositoryNameWithOwner': 'owner/repo', 'branchName': 'feature'}
    assert sent['message']['headline'] == 'test: cover the empty case'
    assert {a['path'] for a in sent['fileChanges']['additions']} == {'tests/test_a.py', 'tests/test_b.py'} and 'deletions' not in sent['fileChanges']
    again = apply(stack, p['plan_id'], p['confirmation'])
    assert again['idempotent_replay'] is True and again['result']['new_sha'] == 'f' * 40
    assert client.graphql.call_count == 1  # a retried call never writes a second commit


def test_deletions_are_sent_as_deletions(stack):
    service, plans, client, repo, head, open_ws = stack
    w = open_ws()['workspace_id']
    service.delete_path(w, OWNER, expected_version=0, path='tests/test_a.py')
    p = plan(stack, w, 1)
    assert p['files'] == [{'path': 'tests/test_a.py', 'status': 'deleted', 'added': 0, 'removed': 2, 'mode': '100644'}]
    client.graphql.return_value = commit_response(p['tree'], head)
    apply(stack, p['plan_id'], p['confirmation'])
    sent = client.graphql.call_args.args[1]['input']['fileChanges']
    assert sent['deletions'] == [{'path': 'tests/test_a.py'}] and sent['additions'] == []


def test_a_workspace_edited_after_planning_cannot_be_published(stack):
    service, plans, client, *_ = stack
    w, p = approved(stack)
    service.write_file(w, OWNER, expected_version=2, path='tests/test_c.py', content='x = 1\n')
    with pytest.raises(SecurityError, match='workspace changed after this plan'):
        apply(stack, p['plan_id'], p['confirmation'])
    client.graphql.assert_not_called()


def test_a_moved_head_is_refused_before_any_write(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.get_pr.return_value['head']['sha'] = 'a' * 40
    with pytest.raises(RevisionMismatchError):
        apply(stack, p['plan_id'], p['confirmation'])
    client.graphql.assert_not_called()
    assert plans.get(p['plan_id'], OWNER)['state'] == 'pending'


def test_plans_are_owner_bound_and_expire(stack, clock):
    service, plans, client, *_ = stack
    w, p = approved(stack)
    for bad in ('pp-' + '0' * 32, '../x', 'pp-short'):
        with pytest.raises(ValidationError, match='Unknown plan_id'):
            apply(stack, bad, 'x')
    with pytest.raises(ValidationError, match='Unknown plan_id'):
        apply(stack, p['plan_id'], p['confirmation'], owner='mallory')
    clock.now += 3601
    with pytest.raises(ValidationError, match='Unknown plan_id'):
        apply(stack, p['plan_id'], p['confirmation'])


def test_a_commit_response_that_does_not_match_the_plan_is_an_uncertain_outcome(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.graphql.return_value = commit_response('9' * 40, head)  # a different tree than planned
    with pytest.raises(GitOperationError) as info:
        apply(stack, p['plan_id'], p['confirmation'])
    assert info.value.diagnostic['outcome'] == 'unknown' and 'same plan' in info.value.message
    assert plans.get(p['plan_id'], OWNER)['state'] == 'unknown_outcome'


def test_a_lost_response_is_reconciled_against_the_real_remote_not_retried(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.graphql.side_effect = APIError('connection reset')
    with pytest.raises(GitOperationError) as info:
        apply(stack, p['plan_id'], p['confirmation'])
    assert info.value.diagnostic['outcome'] == 'unknown' and plans.get(p['plan_id'], OWNER)['state'] == 'unknown_outcome'
    # the write in fact landed: the remote branch now has one child of the head carrying the planned tree
    (repo / 'tests/test_b.py').write_text('def test_b():\n    assert 1 + 1 == 2\n')
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert 1\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'landed')
    landed = run(repo, 'rev-parse', 'HEAD').decode().strip()
    assert run(repo, 'rev-parse', 'HEAD^{tree}').decode().strip() == p['tree']
    client.graphql.reset_mock()
    client.graphql.side_effect = AssertionError('a reconciled plan must not write again')
    done = apply(stack, p['plan_id'], p['confirmation'])
    assert done['state'] == 'applied' and done['result']['new_sha'] == landed and done['result']['reconciled_from_uncertain_attempt'] is True
    client.graphql.assert_not_called()


def test_an_uncertain_plan_whose_write_did_not_land_may_be_applied_once_more(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.graphql.side_effect = APIError('timeout')
    with pytest.raises(GitOperationError):
        apply(stack, p['plan_id'], p['confirmation'])
    client.graphql.side_effect = None
    client.graphql.return_value = commit_response(p['tree'], head)
    done = apply(stack, p['plan_id'], p['confirmation'])  # the remote head is still the expected one, so it is safe to write
    assert done['state'] == 'applied' and done['result']['reconciled_from_uncertain_attempt'] is False and client.graphql.call_count == 2


def test_a_diverged_remote_blocks_an_uncertain_plan(stack):
    service, plans, client, repo, head, open_ws = stack
    w, p = approved(stack)
    client.graphql.side_effect = APIError('timeout')
    with pytest.raises(GitOperationError):
        apply(stack, p['plan_id'], p['confirmation'])
    (repo / 'unrelated.py').write_text('someone else pushed\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'someone else')
    client.graphql.reset_mock()
    client.graphql.side_effect = AssertionError('must not write over a diverged head')
    with pytest.raises(RevisionMismatchError, match='neither the expected head nor this plan'):
        apply(stack, p['plan_id'], p['confirmation'])
    client.graphql.assert_not_called()


# --- contributor forks -------------------------------------------------------------------------------------------

@pytest.fixture
def fork_stack(tmp_path, clock, local_fetch, monkeypatch):
    repo, sha = local_fetch
    (repo / 'tests').mkdir()
    (repo / 'tests/test_a.py').write_text('def test_ok():\n    assert True\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'tests')
    head = run(repo, 'rev-parse', 'HEAD').decode().strip()
    service = WorkspaceService(WorkspaceSettings(data_dir=str(tmp_path / 'ws'), min_free_bytes=64 * 1024 * 1024), clock=clock)
    client = client_for(head, fork=True)
    client.principal = 'maintainer'
    plans = PlanStore(tmp_path / 'plans', clock=clock)
    summary = open_workspace(client, 'owner/repo', service=service, owner=OWNER, number=1, expected_head=head, operations=['read', 'edit', 'diff', 'publish'])
    w = summary['workspace_id']
    service.write_file(w, OWNER, expected_version=0, path='tests/test_b.py', content='def test_b():\n    assert 2\n')
    return service, plans, client, repo, head, w


def test_fork_publication_pushes_a_leased_fast_forward_of_exactly_the_planned_tree(fork_stack, monkeypatch):
    service, plans, client, repo, head, w = fork_stack
    p = plan_publication(client, 'owner/repo', w, 1, 'add test', workspaces=service, plans=plans, jobs=None, owner=OWNER, **LIMITS)
    assert p['target']['is_fork'] is True and any('contributor fork' in l for l in p['limitations'])
    pushed = {}
    original_git = WorkspaceRepo.git

    def fake_push(self, repo_name, ref, expected, new_sha, operation):
        pushed.update(repo=repo_name, ref=ref, expected=expected, new=new_sha, operation=operation,
                      parents=original_git(self, 'rev-list', '--parents', '-n', '1', new_sha).decode().split(),
                      tree=original_git(self, 'rev-parse', f'{new_sha}^{{tree}}').decode().strip(),
                      author=original_git(self, 'log', '-1', '--format=%an <%ae>', new_sha).decode().strip())
        return 'applied'

    def fake_git(self, *args, **kwargs):
        if args[:1] == ('ls-remote',):
            return f'{pushed["new"]}\trefs/heads/{pushed["ref"]}\n'.encode()
        return original_git(self, *args, **kwargs)
    monkeypatch.setattr(WorkspaceRepo, 'push_fast_forward', fake_push)
    monkeypatch.setattr(WorkspaceRepo, 'git', fake_git)
    done = apply_publication(client, 'owner/repo', p['plan_id'], p['confirmation'], True, workspaces=service, plans=plans, jobs=None, owner=OWNER)
    assert done['state'] == 'applied' and pushed['operation'] == 'workspace_publish' and pushed['expected'] == head
    assert pushed['parents'] == [pushed['new'], head] and pushed['tree'] == p['tree'] and pushed['repo'] == 'contributor/repo'
    assert pushed['author'] == 'maintainer <maintainer@users.noreply.github.com>'  # contributor provenance: the authenticated login, not Odytor
    client.graphql.assert_not_called()


def test_a_rejected_fork_push_leaves_the_plan_applicable(fork_stack, monkeypatch):
    service, plans, client, repo, head, w = fork_stack
    p = plan_publication(client, 'owner/repo', w, 1, 'add test', workspaces=service, plans=plans, jobs=None, owner=OWNER, **LIMITS)

    def rejected(self, *a, **k):
        raise GitOperationError('GitHub rejected the push; no commit was added.', {'outcome': 'rejected'})
    monkeypatch.setattr(WorkspaceRepo, 'push_fast_forward', rejected)
    with pytest.raises(GitOperationError) as info:
        apply_publication(client, 'owner/repo', p['plan_id'], p['confirmation'], True, workspaces=service, plans=plans, jobs=None, owner=OWNER)
    assert info.value.diagnostic['outcome'] == 'rejected' and plans.get(p['plan_id'], OWNER)['state'] == 'pending'


def test_the_fork_write_grant_set_includes_publication_and_nothing_broader():
    from odytor.github import FORK_WRITE_OPERATIONS
    assert FORK_WRITE_OPERATIONS == frozenset({'quick_fix', 'workspace_publish'})


def test_cryptographically_bound_approval_token_verification(stack):
    from odytor.publish import verify_approval_token
    service, plans, client, repo, head, open_ws = stack
    w, v = edited(stack)
    p = plan(stack, w, v)
    assert 'approval_token' in p and len(p['approval_token']) == 64
    assert verify_approval_token(plans.signing_key(), p['approval_token'], p['plan_id'], OWNER, 'owner/repo',
                                 p['tree'], p['patch_sha256'], p['expires_at']) is True
    assert verify_approval_token(plans.signing_key(), 'invalid', p['plan_id'], OWNER, 'owner/repo',
                                 p['tree'], p['patch_sha256'], p['expires_at']) is False
    client.graphql.return_value = commit_response(p['tree'], head)
    applied = apply(stack, p['plan_id'], '', approval_token=p['approval_token'])
    assert applied['state'] == 'applied'
    # Replay is idempotent
    replayed = apply(stack, p['plan_id'], '', approval_token=p['approval_token'])
    assert replayed['idempotent_replay'] is True


def test_plan_and_apply_pr_body_draft_only_and_invariants(stack):
    from unittest.mock import MagicMock
    from odytor.publish import plan_pr_body, apply_pr_body
    service, plans, client, repo, head, open_ws = stack

    # Setup PR: open, draft=True
    current_body = "Old PR description"
    client.get_pr.return_value = {
        "number": 5,
        "state": "open",
        "draft": True,
        "title": "Fix something",
        "head": {"sha": head, "ref": "feat"},
        "base": {"sha": "0" * 40, "ref": "main"},
        "body": current_body,
    }

    # 1. Non-draft PR is rejected
    client.get_pr.return_value["draft"] = False
    with pytest.raises(SecurityError, match="permitted on draft pull requests"):
        plan_pr_body(client, "owner/repo", 5, head, "New description", plans=plans, owner=OWNER)

    # 2. Open draft PR creates body plan
    client.get_pr.return_value["draft"] = True
    new_body = "Updated markdown body with audit evidence"
    bp = plan_pr_body(client, "owner/repo", 5, head, new_body, plans=plans, owner=OWNER)
    assert bp["plan_id"].startswith("pb-")
    assert bp["is_draft"] is True
    assert bp["state"] == "pending"
    assert "I approve updating the body of draft PR #5" in bp["confirmation"]

    # 3. Apply requires user_authorized=True
    with pytest.raises(SecurityError, match="user_authorized"):
        apply_pr_body(client, "owner/repo", bp["plan_id"], bp["confirmation"], False, plans=plans, owner=OWNER)

    # 4. Apply requires matching confirmation
    with pytest.raises(SecurityError, match="confirmation does not match"):
        apply_pr_body(client, "owner/repo", bp["plan_id"], "wrong confirm", True, plans=plans, owner=OWNER)

    # 5. Apply re-reads and rejects if body changed concurrently before apply
    client.get_pr.return_value["body"] = "Someone edited this concurrently"
    with pytest.raises(SecurityError, match="PR body changed"):
        apply_pr_body(client, "owner/repo", bp["plan_id"], bp["confirmation"], True, plans=plans, owner=OWNER)

    # Reset body and simulate successful PATCH + read-back
    client.get_pr.return_value["body"] = current_body
    def mock_patch(method, path, json=None, json_data=None, **kwargs):
        payload = json if json is not None else json_data
        if method == "PATCH" and "pulls/5" in path and payload:
            client.get_pr.return_value["body"] = payload.get("body")
            return MagicMock(status_code=200)
        return MagicMock(status_code=200)
    client.request.side_effect = mock_patch

    applied = apply_pr_body(client, "owner/repo", bp["plan_id"], bp["confirmation"], True, plans=plans, owner=OWNER)
    assert applied["status"] == "applied"
    assert applied["invariants_verified"]["title"] is True
    assert applied["invariants_verified"]["base_ref"] is True
    assert applied["invariants_verified"]["head_sha"] is True
    assert applied["invariants_verified"]["draft"] is True
    assert applied["invariants_verified"]["body_matches"] is True

    # 6. Idempotent replay
    replayed = apply_pr_body(client, "owner/repo", bp["plan_id"], bp["confirmation"], True, plans=plans, owner=OWNER)
    assert replayed["idempotent_replay"] is True
