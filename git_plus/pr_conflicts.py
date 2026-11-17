"""Exact Git merge inspection without a checkout or repository code execution."""
from __future__ import annotations

from typing import Any

from git_plus.diagnostics import safe_payload
from git_plus.errors import RevisionMismatchError, ValidationError
from git_plus.git_workspace import GitWorkspace, pr_context, verify_context
from git_plus.github import GitHubClient, validate_sha


def inspect_merge(work: GitWorkspace, base: str, head: str, *, max_paths: int = 100) -> dict[str, Any]:
    """Use merge-tree's stage records, never GitHub's cached mergeability guess."""
    work.git('merge-base', base, head)  # Unrelated histories are an explicit error.
    raw = work.git('-c', 'merge.conflictStyle=diff3', 'merge-tree', '--write-tree', '-z', base, head, allowed=(0, 1))
    fields = raw.split(b'\0')
    tree = validate_sha(fields[0].decode().strip())
    stages: dict[str, dict[str, Any]] = {}
    for field in fields[1:]:
        if not field:
            break
        info, path_bytes = field.split(b'\t', 1)
        mode, oid, stage = info.decode().split()
        path = path_bytes.decode('utf-8', errors='surrogateescape')
        stages.setdefault(path, {})[stage] = {'mode': mode, 'oid': oid}
    conflicts = []
    for path, entries in list(stages.items())[:max_paths]:
        regions = []
        complete = False
        reason = 'structural_or_binary_conflict'
        # Stage absence, non-regular files and binary content have no textual region.
        if set(entries) == {'1', '2', '3'} and all(e['mode'] in {'100644', '100755'} for e in entries.values()):
            size = int(work.git('cat-file', '-s', f'{tree}:{path}').decode())
            if size <= 1048576:
                blob = work.git('show', f'{tree}:{path}')
                try:
                    text = blob.decode('utf-8')
                    if '\0' not in text:
                        # Git's actual conflicted result includes diff3 base regions.
                        lines = text.splitlines(keepends=True)
                        start = None
                        for index, line in enumerate(lines):
                            if line.startswith('<<<<<<< '):
                                start = index
                            elif start is not None and line.startswith('>>>>>>> '):
                                regions.append({'start_line': start + 1, 'end_line': index + 1,
                                                'text': ''.join(lines[start:index+1])})
                                start = None
                        complete = bool(regions) and start is None
                        reason = None if complete else 'no_standard_text_markers'
                except UnicodeDecodeError:
                    pass
            else:
                reason = 'merged_blob_exceeds_1_mib'
        conflicts.append({'path': path, 'stages': entries, 'regions': regions,
                          'regions_complete': complete, 'regions_unavailable_reason': reason})
    return {'conflicting': bool(stages), 'conflicts': conflicts, 'total_conflicting_paths': len(stages),
            'paths_complete': len(stages) <= max_paths,
            'regions_complete': all(c['regions_complete'] for c in conflicts) and len(stages) <= max_paths,
            'merge_tree_sha': tree, 'merge_result_committed': False}


def get_pr_conflicts(client: GitHubClient, repo: str, number: int, expected_head: str,
                     expected_base: str | None = None, max_paths: int = 100) -> dict[str, Any]:
    if type(max_paths) is not int or not 1 <= max_paths <= 100:
        raise ValidationError('max_paths must be between 1 and 100.')
    context = pr_context(client, repo, number, expected_head)
    if expected_base is not None and validate_sha(expected_base) != context['base_sha']:
        raise RevisionMismatchError(expected_base, context['base_sha'], revision='base')
    with GitWorkspace(client) as work:
        work.fetch(context['head_repo'], context['head_ref'], context['head_sha'], 'head')
        # The PR API's base SHA can lag the live branch tip. Preserve the caller's
        # immutable snapshot when that SHA is still an ancestor of the fetched tip.
        # The result must explicitly disclose that it is not a current-tip check.
        observed_base = work.fetch(context['base_repo'], context['base_ref'], context['base_sha'],
                                   'base', allow_base_ancestor=True)
        result = inspect_merge(work, context['base_sha'], context['head_sha'], max_paths=max_paths)
        verify_context(client, repo, number, context, base=True)
    base_is_current = observed_base == context['base_sha']
    return safe_payload({'repo': repo, 'number': number, 'head_sha': context['head_sha'],
                         'base_sha': context['base_sha'], 'base_branch_tip_sha': observed_base,
                         'base_snapshot_is_current': base_is_current,
                         'current_base_conflicts_checked': base_is_current,
                         'merge_scope': 'captured_pr_base_sha', **result})
