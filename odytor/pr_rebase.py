"""Real linear-history rebasing in disposable Git workspaces."""
from __future__ import annotations

from typing import Any

from odytor.diagnostics import safe_payload
from odytor.errors import GitOperationError, RevisionMismatchError, SecurityError, ValidationError
from odytor.github import GitHubClient, validate_sha
from odytor.git_workspace import GitWorkspace, pr_context, verify_context


def rebase_pr(client: GitHubClient, repo: str, number: int, expected_head: str, expected_base: str,
              *, apply: bool = False, allow_history_rewrite: bool = False) -> dict[str, Any]:
    """Replay linear commits onto an exact base revision; apply only with explicit consent.

    expected_base must be the live tip of the PR's target branch (verified by fetching it, and again immediately
    before an applied push). GitHub's recorded PR base can lag the branch, which is exactly when a PR needs
    rebasing, so that snapshot is reported but is not required to match.

    Existing empty commits and newly empty commits are preserved. Merge histories
    are rejected. Signatures on rewritten commits cannot be preserved. No tests run.
    """
    if apply and not allow_history_rewrite:
        raise SecurityError('Applying a rebase requires allow_history_rewrite=true.')
    expected_base = validate_sha(expected_base, 'expected_base')
    context = pr_context(client, repo, number, expected_head, write=apply)
    result: dict[str, Any] = {'repo': repo, 'number': number, 'head_repo': context['head_repo'],
        'head_ref': context['head_ref'], 'old_sha': context['head_sha'], 'base_sha': expected_base,
        'base_sha_role': 'live_target_branch_tip', 'pr_api_base_snapshot': context['base_sha'],
        'applied': False, 'validation': {'tests_run': False},
        'base_revision_scope': 'exact live base branch tip, rechecked immediately before push'}
    with GitWorkspace(client) as work:
        work.fetch(context['head_repo'], context['head_ref'], context['head_sha'], 'head')
        work.fetch(context['base_repo'], context['base_ref'], expected_base, 'base')
        merge_bases = work.git('merge-base', '--all', expected_base, context['head_sha']).decode().splitlines()
        if len(merge_bases) != 1:
            raise ValidationError('Rebase requires exactly one common merge base.')
        merge_base = merge_bases[0]
        if merge_base == expected_base:
            verify_context(client, repo, number, context, write=apply, base=True)
            return {**result, 'status': 'up_to_date', 'new_sha': context['head_sha'], 'rewritten_commits': []}
        old_range = f'{merge_base}..{context["head_sha"]}'
        if work.git('rev-list', '--merges', old_range).strip():
            raise ValidationError('Merge commits are unsupported; refusing to flatten history.')
        originals = work.git('rev-list', '--reverse', old_range).decode().splitlines()
        if not originals or len(originals) > 200:
            raise ValidationError('Rebase requires between 1 and 200 linear PR commits.')
        work.git('checkout', '--quiet', '--detach', context['head_sha'])
        try:
            work.git('rebase', '--onto', expected_base, merge_base, '--no-verify', '--no-autosquash',
                     '--no-autostash', '--no-update-refs', '--reapply-cherry-picks', '--keep-empty', '--empty=keep')
        except GitOperationError as exc:
            paths = work.git('diff', '--name-only', '--diff-filter=U', '-z').decode(errors='replace').strip('\0').split('\0')
            # The entire workspace is discarded after returning. No remote writes.
            if paths != ['']:
                return safe_payload({**result, 'status': 'conflicts', 'conflicting_paths': paths,
                                     'paths_complete': True, 'regions_complete': False})
            raise GitOperationError('Rebase failed before push; temporary workspace discarded.') from exc
        new_sha = validate_sha(work.git('rev-parse', 'HEAD').decode().strip())
        rewritten = work.git('rev-list', '--reverse', f'{expected_base}..{new_sha}').decode().splitlines()
        if len(rewritten) != len(originals):
            raise GitOperationError('Rebase unexpectedly changed the number of commits; refusing to push.')
        # Prove a direct, linear chain rooted at the exact captured base.
        parent = expected_base
        for oid in rewritten:
            parents = work.git('show', '-s', '--format=%P', oid).decode().strip().split()
            if parents != [parent]:
                raise GitOperationError('Rebased parent chain does not match the captured base.')
            parent = oid
        result.update(status='rebased', new_sha=new_sha,
                      rewritten_commits=[{'old_sha': old, 'new_sha': new} for old, new in zip(originals, rewritten)],
                      signatures_preserved=False)
        verify_context(client, repo, number, context, write=apply, base=True)
        if apply:
            live = work.git('ls-remote', '--refs', '--', work.url(context['base_repo']), f'refs/heads/{context["base_ref"]}',
                            network=True, credential_repo=context['base_repo']).decode().split()
            if not live or live[0] != expected_base:
                raise RevisionMismatchError(expected_base, live[0] if live else '', 'Base branch moved since the rebase was built; nothing was pushed.', revision='base')
            ref = f'refs/heads/{context["head_ref"]}'
            lease = f'--force-with-lease={ref}:{context["head_sha"]}'
            try:
                work.git('push', '--porcelain', lease, '--', work.url(context['head_repo']), f'{new_sha}:{ref}', network=True, credential_repo=context['head_repo'])
            except GitOperationError as exc:
                raise GitOperationError('Rebase push failed or response was lost. Reconcile the branch before retrying.', {
                    'outcome': 'unknown', 'old_sha': context['head_sha'], 'new_sha': new_sha,
                    'head_repo': context['head_repo'], 'head_ref': context['head_ref'],
                }) from exc
            try:
                remote = work.git('ls-remote', '--refs', '--', work.url(context['head_repo']), ref, network=True, credential_repo=context['head_repo']).decode().split()
            except GitOperationError as exc:
                raise GitOperationError('Push accepted, remote verification unavailable. Reconcile before retrying.', {
                    'outcome': 'unknown', 'new_sha': new_sha,
                }) from exc
            if not remote or remote[0] != new_sha:
                raise GitOperationError('Push returned success but remote revision differs; reconcile before retrying.', {
                    'outcome': 'unknown', 'new_sha': new_sha, 'observed_sha': remote[0] if remote else None,
                })
            result['applied'] = True
            result['remote_verified'] = True
    return safe_payload(result)
