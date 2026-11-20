"""Guarded publication: turn a validated workspace change into one additive commit on a PR head, only when authorized.

Publication is two separate acts so that nothing reaches GitHub because tests happened to pass:

  plan   read-only. Binds the exact workspace version and tree, the complete patch digest, the expected remote
         head, the commit message, the validation evidence for that very tree and the known limitations. It
         changes nothing and returns the phrase an authorizer must repeat.
  apply  the only write. It refuses unless the caller states user authorization and quotes the plan's
         confirmation (which names the tree and patch digest), the workspace still holds exactly the planned tree,
         the recomputed patch digest equals the planned one, and the PR head is still the planned revision.

The write itself reuses the mechanisms quick_fix already earned its guarantees with: GitHub's atomic
`expectedHeadOid` for same-repository branches, a leased fast-forward push for contributor forks, the same path
policy on every changed file, no force push and no history rewrite. A plan is applied at most once. Intent is
recorded durably *before* the write; if the outcome of a write is uncertain, the next apply reconciles against the
real remote (is the head now a single child of the expected head carrying the planned tree?) instead of retrying
blindly, so a lost response can never produce a duplicate commit.

Nothing here merges, approves, comments, dispatches workflows or touches any branch but the PR head.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from odytor.diagnostics import safe_text
from odytor.errors import APIError, GitOperationError, GitPlusError, RevisionMismatchError, SecurityError, ValidationError
from odytor.git_workspace import pr_context
from odytor.github import validate_repo, validate_sha
from odytor.quick_fix import PatchSummary, _CREATE_COMMIT, _commit_identity, _validate_path
from odytor.workspace import WorkspaceRepo, WorkspaceService

PLAN_ID = re.compile(r"^(?:pp|pb)-[0-9a-f]{32}$")
SCHEMA = 1
OPERATION = "workspace_publish"
PLAN_TTL_SECONDS = 3600
APPLIED_RETENTION_SECONDS = 24 * 3600
INLINE_PATCH_BYTES = 48 * 1024
MAX_COMMIT_MESSAGE_BYTES = 1440
MAX_BLOB_TOTAL = 1048576
_REGULAR = {"100644", "100755"}


def new_plan_id() -> str:
    return "pp-" + os.urandom(16).hex()


class PlanStore:
    """One atomic JSON file per plan; owner-bound; unapplied plans expire quickly, applied ones are kept for idempotency."""

    def __init__(self, directory: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.root, self.clock = Path(directory), clock
        self._lock = threading.RLock()

    def signing_key(self) -> bytes:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            key_file = self.root / ".signing_key"
            if key_file.exists():
                try:
                    k = key_file.read_bytes()
                    if len(k) >= 32:
                        return k
                except OSError:
                    pass
            new_key = os.urandom(32)
            tmp = key_file.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(new_key)
            os.replace(tmp, key_file)
            return new_key

    def _path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
            raise ValidationError("Unknown plan_id.")
        return self.root / f"{plan_id}.json"

    def save(self, plan: dict[str, Any]) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self._path(plan["plan_id"])
            tmp = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(plan, handle, separators=(",", ":"))
            os.replace(tmp, target)

    def get(self, plan_id: str, owner: str) -> dict[str, Any]:
        with self._lock:
            self.purge()
            try:
                plan = json.loads(self._path(plan_id).read_text())
            except (OSError, ValueError):
                plan = None
            if plan is None or plan["owner"] != owner:
                raise ValidationError("Unknown plan_id.")
            return plan

    def purge(self) -> None:
        now = self.clock()
        if not self.root.exists():
            return
        for path in self.root.glob("*.json"):
            if not PLAN_ID.fullmatch(path.stem):
                continue
            try:
                plan = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            keep_until = plan["expires_at"] if plan["state"] == "pending" else plan.get("applied_at", plan["created_at"]) + APPLIED_RETENTION_SECONDS
            if plan["state"] in {"pending", "applied"} and keep_until <= now:
                path.unlink(missing_ok=True)


def _iso(epoch: float | None) -> str | None:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds") if epoch else None


def confirmation_phrase(plan_id: str, tree: str, patch_sha256: str) -> str:
    """What the authorizer must quote back: it names the plan, the exact tree and the patch digest."""
    return f"publish {plan_id} tree {tree[:12]} patch {patch_sha256[:12]}"


def _normalize_expiry(expires_at: float | str) -> int:
    if isinstance(expires_at, (int, float)):
        return int(expires_at)
    import datetime as dt
    try:
        return int(dt.datetime.fromisoformat(str(expires_at)).timestamp())
    except Exception:
        return 0


def make_approval_token(key: bytes, plan_id: str, owner: str, repo: str, tree: str, patch_sha256: str, expires_at: float | str) -> str:
    msg = f"{plan_id}:{owner}:{repo}:{tree}:{patch_sha256}:{_normalize_expiry(expires_at)}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_approval_token(key: bytes, token: str, plan_id: str, owner: str, repo: str, tree: str, patch_sha256: str, expires_at: float | str) -> bool:
    if not isinstance(token, str) or not token:
        return False
    expected = make_approval_token(key, plan_id, owner, repo, tree, patch_sha256, expires_at)
    return hmac.compare_digest(token, expected)


def _changed_files(repository: WorkspaceRepo, base: str, tree: str) -> list[dict[str, Any]]:
    """Per-file status and modes between two trees; anything but a regular-file add, modify or delete is refused."""
    raw = repository.git("diff", "--raw", "--no-renames", "-z", "--no-ext-diff", "--no-textconv", base, tree).split(b"\0")
    stats: dict[str, tuple[int | None, int | None]] = {}
    for item in filter(None, repository.git("diff", "--no-renames", "--numstat", "-z", base, tree).split(b"\0")):
        a, r, name = item.decode("utf-8", errors="replace").split("\t", 2)
        stats[name] = (None if a == "-" else int(a), None if r == "-" else int(r))
    files = []
    for meta, name in zip(raw[0::2], raw[1::2]):
        if not meta:
            break
        old_mode, new_mode, _old, _new, status = meta.decode().lstrip(":").split()
        path = name.decode("utf-8", errors="replace")
        code = status[0]
        if code not in "AMD":
            raise SecurityError(f"{path}: only added, modified or deleted regular files can be published (found status {status}).")
        if code == "A" and new_mode != "100644":
            raise SecurityError(f"{path}: new files must be regular, non-executable (mode 100644), found {new_mode}.")
        if code == "M" and (old_mode != new_mode or new_mode not in _REGULAR):
            raise SecurityError(f"{path}: mode changes and non-regular files cannot be published ({old_mode} to {new_mode}).")
        if code == "D" and old_mode not in _REGULAR:
            raise SecurityError(f"{path}: only regular files can be deleted.")
        added, removed = stats.get(path, (None, None))
        if code != "D" and added is None:
            raise SecurityError(f"{path}: binary content cannot be published.")
        files.append({"path": path, "status": {"A": "added", "M": "modified", "D": "deleted"}[code], "added": added or 0, "removed": removed or 0,
                      "mode": new_mode if code != "D" else old_mode})
    return files


def _policy_violations(paths: list[str]) -> list[dict[str, str]]:
    found = []
    for path in paths:
        try:
            _validate_path(path)
        except GitPlusError as exc:
            found.append({"path": path, "reason": exc.message})
    return found


def _validation_evidence(jobs: Any | None, workspace: dict[str, Any], tree: str) -> dict[str, Any]:
    """Runs on exactly this tree, from the job store; runs on any other tree are counted, never mixed in."""
    on_tree, other = [], 0
    for run_id in workspace.get("runs", []):
        record = jobs.store.load(run_id) if jobs is not None else None
        if record is None:
            continue
        if (record.get("trees") or {}).get("effective_tree") == tree:
            on_tree.append({"run_id": run_id, "profile": record.get("profile"), "kind": record.get("kind"), "status": record.get("status"),
                            "green": record.get("status") == "passed",
                            "workspace_version": (record.get("revisions") or {}).get("workspace_version")})
        else:
            other += 1
    return {"runs_on_this_tree": on_tree, "runs_on_other_trees": other,
            "any_green_on_this_tree": any(r["green"] for r in on_tree),
            "all_finished_runs_green": bool(on_tree) and all(r["green"] for r in on_tree if r["status"] in {"passed", "failed", "setup_error", "timeout"}),
            "runs_not_green": sum(1 for r in on_tree if r["status"] in {"failed", "setup_error", "timeout", "no_tests_collected", "no_tests_executed"})}


def plan_publication(client: Any, repo: str, workspace_id: str, expected_version: int, commit_message: str, *, workspaces: WorkspaceService,
                     plans: PlanStore, jobs: Any | None, owner: str, max_files: int, max_changed_lines: int, max_patch_bytes: int) -> dict[str, Any]:
    """Prepare (never perform) a publication. Read-only: nothing is written anywhere."""
    validate_repo(repo)
    record = workspaces.get(workspace_id, owner, operation="publish")
    if record["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown workspace_id.")
    if type(expected_version) is not int or expected_version != record["version"]:
        from odytor.workspace import VersionConflictError
        raise VersionConflictError(expected_version, record["version"], record["tree"])
    source = record["source"]
    if source.get("kind") != "pr_head" or record.get("pr") is None:
        raise ValidationError("Only a workspace opened from a PR head (source='pr_head') can be published. Merge and branch "
                              "workspaces are for reading and testing; publishing them would add merge content or write a branch directly.",
                              {"source_kind": source.get("kind")})
    if not isinstance(commit_message, str) or not commit_message.strip() or len(commit_message.encode()) > MAX_COMMIT_MESSAGE_BYTES or "\0" in commit_message:
        raise ValidationError(f"The commit message must be 1-{MAX_COMMIT_MESSAGE_BYTES} bytes without NUL.")
    base_tree, tree = record["source_tree"], record["tree"]
    if base_tree == tree:
        raise ValidationError("The workspace has no changes against its source revision; there is nothing to publish.")
    repository = workspaces._repo(workspace_id)
    files = _changed_files(repository, base_tree, tree)
    if len(files) > max_files:
        raise ValidationError(f"The change touches {len(files)} files; the publication limit is {max_files}.", {"stage": "limits"})
    lines = sum(f["added"] + f["removed"] for f in files)
    if lines > max_changed_lines:
        raise ValidationError(f"The change touches {lines} lines; the publication limit is {max_changed_lines}.", {"stage": "limits"})
    violations = _policy_violations([f["path"] for f in files])
    if violations:
        raise SecurityError("Some changed paths are not publishable under the repository write policy.", {"stage": "path_policy", "violations": violations[:20]})
    patch = workspaces.canonical_patch(workspace_id, base_tree, tree)
    if len(patch) > max_patch_bytes:
        raise ValidationError(f"The complete patch is {len(patch)} bytes; the publication limit is {max_patch_bytes}.", {"stage": "limits"})
    total = 0
    for f in files:
        if f["status"] != "deleted":
            blob = repository.git("cat-file", "blob", f"{tree}:{f['path']}")
            total += len(blob)
            if b"\0" in blob:
                raise SecurityError(f"{f['path']}: binary content cannot be published.")
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(f"{f['path']} is not UTF-8 text.") from exc
    if total > MAX_BLOB_TOTAL:
        raise ValidationError("The changed files exceed 1 MiB in total.", {"stage": "limits"})
    head = source["head_sha"]
    pr = client.get_pr(repo, record["pr"])
    live_head = ((pr.get("head") or {}).get("sha") or "").lower()
    if pr.get("state") != "open" or pr.get("merged"):
        raise ValidationError("The pull request is no longer open.")
    if live_head != head:
        raise RevisionMismatchError(head, live_head, "The PR head moved since this workspace was opened; open a new workspace on the new head.")
    fork = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "").casefold() != repo.casefold()
    evidence = _validation_evidence(jobs, record, tree)
    limitations = ["The commit is additive: one new commit on top of the PR head. No history is rewritten and nothing is force-pushed.",
                   "Nothing is merged, approved, commented or dispatched; CI on the PR head reruns by itself on push."]
    if not evidence["runs_on_this_tree"]:
        limitations.append("No test run exists for this exact tree. Run workspace_run on this version before authorizing, or accept that it is untested.")
    elif not evidence["any_green_on_this_tree"]:
        limitations.append("No run on this exact tree passed.")
    elif evidence["runs_not_green"]:
        limitations.append(f"{evidence['runs_not_green']} run(s) on this exact tree did not pass (see validation.runs_on_this_tree); a passing run "
                           "alongside them is not a clean result, so read those runs before authorizing.")
    if fork:
        limitations.append("The PR head lives in a contributor fork: the push needs GitHub-confirmed maintainer access to that branch and is made "
                           "as a leased fast-forward; the commit will not be GitHub-signed.")
    now = workspaces.clock()
    plan_id = new_plan_id()
    approval_token = make_approval_token(plans.signing_key(), plan_id, owner, repo, tree, hashlib.sha256(patch).hexdigest(), now + PLAN_TTL_SECONDS)
    plan = {"schema": SCHEMA, "plan_id": plan_id, "owner": owner, "repo": repo, "state": "pending", "workspace_id": workspace_id,
            "workspace_version": record["version"], "tree": tree, "base_tree": base_tree, "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_bytes": len(patch), "files": files, "commit_message": commit_message.strip(),
            "target": {"kind": "pr_head", "number": record["pr"], "head_sha": head, "head_repo": source["head_repo"], "head_ref": source["head_ref"],
                       "is_fork": fork},
            "validation": evidence, "limitations": limitations, "created_at": now, "expires_at": now + PLAN_TTL_SECONDS,
            "approval_token": approval_token}
    plan["confirmation"] = confirmation_phrase(plan_id, tree, plan["patch_sha256"])
    plans.save(plan)
    return public_plan(plan, patch)


def public_plan(plan: dict[str, Any], patch: bytes | None = None) -> dict[str, Any]:
    out = {k: plan[k] for k in ("plan_id", "state", "repo", "workspace_id", "workspace_version", "tree", "base_tree", "patch_sha256", "patch_bytes",
                                "files", "commit_message", "target", "validation", "limitations", "confirmation")}
    if "approval_token" in plan:
        out["approval_token"] = plan["approval_token"]
    out.update(created_at=_iso(plan["created_at"]), expires_at=_iso(plan["expires_at"]), github_mutations="none until publish_apply succeeds",
               required_authorization={"user_authorized": True, "confirm": plan["confirmation"],
                                       "approval_token": plan.get("approval_token"),
                                       "note": "Show this plan to the user. Call publish_apply only with their explicit approval, passing "
                                               "user_authorized=true and the confirm text or approval_token above."})
    if plan.get("result"):
        out["result"] = plan["result"]
    if patch is not None:
        if len(patch) <= INLINE_PATCH_BYTES:
            out["patch"] = patch.decode("utf-8", errors="replace")
        else:
            out["patch_omitted"] = "The complete patch is larger than the inline limit; page it with workspace_diff (its patch_sha256 equals this plan's)."
    return out


def _blobs(repository: WorkspaceRepo, tree: str, files: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    additions, deletions = [], []
    for f in files:
        if f["status"] == "deleted":
            deletions.append({"path": f["path"]})
        else:
            blob = repository.git("cat-file", "blob", f"{tree}:{f['path']}")
            additions.append({"path": f["path"], "contents": base64.b64encode(blob).decode()})
    return additions, deletions


def _reconcile(repository: WorkspaceRepo, plan: dict[str, Any]) -> dict[str, Any]:
    """Decide from the real remote whether an earlier attempt landed: a single child of the expected head with the planned tree."""
    target = plan["target"]
    tip = repository.fetch_tip(target["head_repo"], target["head_ref"], "head")
    if tip == target["head_sha"]:
        return {"state": "not_applied", "remote_head": tip}
    lineage = repository.git("rev-list", "--parents", "-n", "1", tip).decode().split()
    tip_tree = repository.git("rev-parse", f"{tip}^{{tree}}").decode().strip()
    if lineage == [tip, target["head_sha"]] and tip_tree == plan["tree"]:
        return {"state": "applied", "remote_head": tip}
    return {"state": "diverged", "remote_head": tip}


def apply_publication(client: Any, repo: str, plan_id: str, confirm: str, user_authorized: bool, *, workspaces: WorkspaceService,
                      plans: PlanStore, jobs: Any | None, owner: str, approval_token: str | None = None) -> dict[str, Any]:
    """The only GitHub write in this module. Every precondition is re-checked against live state, not against the plan alone."""
    validate_repo(repo)
    plan = plans.get(plan_id, owner)
    if plan["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown plan_id.")
    if plan["state"] == "applied":
        return {**public_plan(plan), "idempotent_replay": True, "note": "This plan was already applied; nothing was written again."}
    if user_authorized is not True:
        raise SecurityError("Publishing writes to GitHub and needs explicit user approval. Pass user_authorized=true only after the user "
                            "approved this exact plan.", {"required": "user_authorized"})
    token_valid = False
    if approval_token:
        token_valid = verify_approval_token(plans.signing_key(), approval_token, plan_id, owner, plan["repo"],
                                            plan["tree"], plan["patch_sha256"], plan["expires_at"])
    confirm_valid = isinstance(confirm, str) and (
        confirm == plan["confirmation"] or
        confirm == confirmation_phrase(plan_id, plan["tree"], plan["patch_sha256"])
    )
    if not token_valid and not confirm_valid:
        raise SecurityError("The confirmation does not match this plan. It must quote the plan's confirmation text exactly, which names "
                            "the tree and patch digest the user approved.", {"required": "confirm"})
    if plan["state"] == "pending" and plan["expires_at"] <= workspaces.clock():
        raise ValidationError("This plan expired; create a new plan from the workspace.")
    if plan["state"] not in {"pending", "unknown_outcome"}:
        raise ValidationError(f"This plan is in state '{plan['state']}' and cannot be applied.")
    record = workspaces.get(plan["workspace_id"], owner, operation="publish")
    if record["version"] != plan["workspace_version"] or record["tree"] != plan["tree"]:
        raise SecurityError("The workspace changed after this plan was made. Create a new plan for the current tree.",
                            {"planned_version": plan["workspace_version"], "current_version": record["version"]})
    repository = workspaces._repo(plan["workspace_id"])
    repository.client = client
    patch = workspaces.canonical_patch(plan["workspace_id"], plan["base_tree"], plan["tree"])
    if hashlib.sha256(patch).hexdigest() != plan["patch_sha256"]:
        raise SecurityError("The recomputed patch does not match the planned digest; refusing to publish.")
    files = _changed_files(repository, plan["base_tree"], plan["tree"])
    if files != plan["files"]:
        raise SecurityError("The changed-file list no longer matches the plan; refusing to publish.")
    if _policy_violations([f["path"] for f in files]):
        raise SecurityError("The planned paths are no longer publishable under the repository write policy.")
    target = plan["target"]
    if plan["state"] == "unknown_outcome":
        verdict = _reconcile(repository, plan)
        if verdict["state"] == "applied":
            return _finish(plans, plan, verdict["remote_head"], target, reconciled=True, jobs=jobs, workspaces=workspaces)
        if verdict["state"] == "diverged":
            raise RevisionMismatchError(target["head_sha"], verdict["remote_head"],
                                        "The PR head is neither the expected head nor this plan's commit; nothing was written. Inspect the PR and re-plan.")
    context = pr_context(client, repo, target["number"], target["head_sha"], write=True, operation=OPERATION)
    if context["head_repo"].casefold() != target["head_repo"].casefold() or context["head_ref"] != target["head_ref"]:
        raise SecurityError("The PR now targets a different head branch than the plan.")
    plan["state"] = "applying"
    plan["attempts"] = plan.get("attempts", 0) + 1
    plans.save(plan)  # intent is durable before the write: a crash from here on is reconciled, never blindly retried
    try:
        if context["is_fork"]:
            new_sha = _push_fork(repository, client, repo, plan, context)
        else:
            new_sha = _create_commit(client, plan, context, repository)
    except (APIError, GitOperationError) as exc:
        outcome = (exc.diagnostic or {}).get("outcome", "unknown") if isinstance(exc, GitOperationError) else "unknown"
        plan["state"] = "pending" if outcome == "rejected" else "unknown_outcome"
        plans.save(plan)
        diag = {**(exc.diagnostic or {}), "outcome": outcome, "plan_id": plan_id, "plan_state": plan["state"]}
        message = ("GitHub rejected the write; nothing changed. The plan can be applied again." if outcome == "rejected" else
                   "The outcome of the write is uncertain. Do not create a new plan: call publish_apply again with the same plan and it will "
                   "reconcile against the real remote before doing anything.")
        raise GitOperationError(message, diag) from exc
    return _finish(plans, plan, new_sha, target, reconciled=False, jobs=jobs, workspaces=workspaces)


def _create_commit(client: Any, plan: dict[str, Any], context: dict[str, Any], repository: WorkspaceRepo) -> str:
    additions, deletions = _blobs(repository, plan["tree"], plan["files"])
    headline, _, body = plan["commit_message"].partition("\n")
    data = client.graphql(_CREATE_COMMIT, {"input": {
        "branch": {"repositoryNameWithOwner": context["head_repo"], "branchName": context["head_ref"]},
        "expectedHeadOid": context["head_sha"], "message": {"headline": headline, "body": body},
        "fileChanges": {"additions": additions, **({"deletions": deletions} if deletions else {})}}})
    commit = (data.get("createCommitOnBranch") or {}).get("commit") or {}
    parents = commit.get("parents") or {}
    if (commit.get("tree", {}).get("oid") != plan["tree"] or parents.get("totalCount") != 1
            or parents.get("nodes") != [{"oid": context["head_sha"]}]):
        raise GitOperationError("The commit response did not match the plan; reconcile before retrying.",
                                {"outcome": "unknown", "commit": commit.get("oid"), "expected_tree": plan["tree"], "observed_tree": (commit.get("tree") or {}).get("oid")})
    return validate_sha(commit.get("oid", ""))


def _push_fork(repository: WorkspaceRepo, client: Any, repo: str, plan: dict[str, Any], context: dict[str, Any]) -> str:
    login, email = _commit_identity(client)
    repository.identity = (login, email)
    head_repo, head_ref, head_sha = context["head_repo"], context["head_ref"], context["head_sha"]
    new_sha = validate_sha(repository.git("commit-tree", plan["tree"], "-p", head_sha, "-m", plan["commit_message"]).decode().strip())
    repository.push_fast_forward(head_repo, head_ref, head_sha, new_sha, OPERATION)
    try:
        remote = repository.git("ls-remote", "--refs", "--", repository.url(head_repo), f"refs/heads/{head_ref}", network=True,
                                credential_repo=head_repo).decode().split()
    except GitOperationError as exc:
        raise GitOperationError("Push accepted, remote verification unavailable. Reconcile before retrying.", {"outcome": "unknown", "new_sha": new_sha}) from exc
    if not remote or remote[0] != new_sha:
        raise GitOperationError("Push returned success but the remote revision differs; reconcile before retrying.",
                                {"outcome": "unknown", "new_sha": new_sha, "observed_sha": remote[0] if remote else None})
    return new_sha


def _finish(plans: PlanStore, plan: dict[str, Any], new_sha: str, target: dict[str, Any], *, reconciled: bool, jobs: Any | None,
            workspaces: WorkspaceService) -> dict[str, Any]:
    plan["state"] = "applied"
    plan["applied_at"] = workspaces.clock()
    plan["result"] = {"new_sha": new_sha, "expected_head": target["head_sha"], "head_repo": target["head_repo"], "head_ref": target["head_ref"],
                      "tree_sha": plan["tree"], "commit_url": f"https://github.com/{target['head_repo']}/commit/{new_sha}",
                      "changed_files": [f["path"] for f in plan["files"]], "patch_sha256": plan["patch_sha256"],
                      "reconciled_from_uncertain_attempt": reconciled, "remote_verified": True, "force_push": False, "history_rewritten": False,
                      "validation_at_apply": {"tests_run_by_this_call": False, **plan["validation"]}}
    plans.save(plan)
    ws_id = plan.get("workspace_id")
    owner = plan.get("owner")
    if ws_id and owner and hasattr(workspaces, "close"):
        try:
            workspaces.close(ws_id, owner)
        except Exception:
            pass
    return public_plan(plan)


def new_body_plan_id() -> str:
    return "pb-" + os.urandom(16).hex()


def confirmation_phrase_body(plan_id: str, repo: str, number: int, current_digest: str, replacement_digest: str) -> str:
    return (
        f"I approve updating the body of draft PR #{number} on {repo} "
        f"(plan {plan_id}, current digest {current_digest[:8]}, replacement digest {replacement_digest[:8]})"
    )


def plan_pr_body(
    client: Any,
    repo: str,
    number: int,
    expected_head: str,
    body: str,
    *,
    plans: PlanStore,
    owner: str,
) -> dict[str, Any]:
    """Prepare a body-only update plan for a draft PR. Read-only: nothing is written to GitHub."""
    validate_repo(repo)
    if type(number) is not int or number <= 0:
        raise ValidationError(f"Pull request number must be positive (got {number}).")
    expected_head = validate_sha(expected_head, "expected_head").lower()
    if not isinstance(body, str):
        raise ValidationError("body must be a string.")
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > 65536:
        raise ValidationError(f"Replacement body exceeds limit of 65536 bytes (got {len(body_bytes)} bytes).")

    pr = client.get_pr(repo, number)
    if pr.get("state") != "open" or pr.get("merged"):
        raise ValidationError("The pull request is no longer open.")
    if pr.get("draft") is not True:
        raise SecurityError(
            "pr_body operations are only permitted on draft pull requests to protect public review context.",
            {"draft": pr.get("draft", False)},
        )
    live_head = ((pr.get("head") or {}).get("sha") or "").lower()
    if live_head != expected_head:
        raise RevisionMismatchError(expected_head, live_head, "The PR head moved; inspect the PR before planning.")

    current_body = pr.get("body") or ""
    current_digest = hashlib.sha256(current_body.encode("utf-8")).hexdigest()
    replacement_digest = hashlib.sha256(body_bytes).hexdigest()

    plan_id = new_body_plan_id()
    now = plans.clock()
    expires_at = now + PLAN_TTL_SECONDS
    confirm = confirmation_phrase_body(plan_id, repo, number, current_digest, replacement_digest)

    plan = {
        "schema": SCHEMA,
        "kind": "pr_body",
        "plan_id": plan_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "expected_head": expected_head,
        "expected_draft": True,
        "current_body_digest": current_digest,
        "replacement_body": body,
        "replacement_body_digest": replacement_digest,
        "state": "pending",
        "created_at": now,
        "expires_at": expires_at,
        "confirmation": confirm,
    }
    plans.save(plan)

    return {
        "plan_id": plan_id,
        "kind": "pr_body",
        "repo": repo,
        "number": number,
        "expected_head": expected_head,
        "is_draft": True,
        "current_body_digest": current_digest,
        "replacement_body_digest": replacement_digest,
        "body_preview": safe_text(body, 500),
        "confirmation": confirm,
        "expires_at": _iso(expires_at),
        "state": "pending",
    }


def apply_pr_body(
    client: Any,
    repo: str,
    plan_id: str,
    confirm: str,
    user_authorized: bool,
    *,
    plans: PlanStore,
    owner: str,
) -> dict[str, Any]:
    """Apply an approved body update plan to a draft PR with strict invariant verification."""
    validate_repo(repo)
    plan = plans.get(plan_id, owner)
    if plan.get("kind") != "pr_body" or plan["repo"].casefold() != repo.casefold():
        raise ValidationError("Unknown plan_id.")
    if plan["state"] == "applied":
        return {
            "plan_id": plan_id,
            "kind": "pr_body",
            "repo": repo,
            "number": plan["number"],
            "status": "applied",
            "idempotent_replay": True,
            "note": "This plan was already applied; nothing was written again.",
        }
    if user_authorized is not True:
        raise SecurityError(
            "Updating the PR body writes to GitHub and needs explicit user approval. Pass user_authorized=true only after the user approved this exact plan.",
            {"required": "user_authorized"},
        )
    if not isinstance(confirm, str) or confirm != plan["confirmation"]:
        raise SecurityError(
            "The confirmation does not match this plan. It must quote the plan's confirmation text exactly.",
            {"required": "confirm"},
        )
    if plan["expires_at"] <= plans.clock():
        raise ValidationError("This plan expired; create a new body plan.")
    if plan["state"] != "pending":
        raise ValidationError(f"This plan is in state '{plan['state']}' and cannot be applied.")

    number = plan["number"]
    # Re-read PR before write
    pr = client.get_pr(repo, number)
    if pr.get("state") != "open" or pr.get("merged"):
        raise ValidationError("The pull request is no longer open.")
    if pr.get("draft") is not True:
        raise SecurityError("The PR is no longer a draft; body update aborted.")
    live_head = ((pr.get("head") or {}).get("sha") or "").lower()
    if live_head != plan["expected_head"].lower():
        raise RevisionMismatchError(plan["expected_head"], live_head, "PR head moved before body update.")

    live_body = pr.get("body") or ""
    live_digest = hashlib.sha256(live_body.encode("utf-8")).hexdigest()
    if live_digest != plan["current_body_digest"]:
        raise SecurityError("The PR body changed since this plan was created; re-plan with current body.")

    orig_title = pr.get("title")
    orig_base_ref = (pr.get("base") or {}).get("ref")
    orig_base_sha = (pr.get("base") or {}).get("sha")

    # Update body
    try:
        client.request("PATCH", f"repos/{repo}/pulls/{number}", json_data={"body": plan["replacement_body"]})
    except Exception as exc:
        raise GitOperationError(f"GitHub rejected the body update: {exc}") from exc

    # Immediate post-write read-back to verify invariants
    after_pr = client.get_pr(repo, number)
    after_head = ((after_pr.get("head") or {}).get("sha") or "").lower()
    after_base_ref = (after_pr.get("base") or {}).get("ref")
    after_base_sha = (after_pr.get("base") or {}).get("sha")
    after_draft = after_pr.get("draft") is True
    after_title = after_pr.get("title")
    after_body = after_pr.get("body") or ""

    invariants_ok = (
        after_head == plan["expected_head"].lower()
        and after_base_ref == orig_base_ref
        and after_base_sha == orig_base_sha
        and after_draft is True
        and after_title == orig_title
        and after_body == plan["replacement_body"]
    )
    if not invariants_ok:
        raise GitOperationError("Invariant violation after PR body update: title, base, head, or draft state changed unexpectedly.")

    plan["state"] = "applied"
    plan["applied_at"] = plans.clock()
    plans.save(plan)

    return {
        "plan_id": plan_id,
        "kind": "pr_body",
        "repo": repo,
        "number": number,
        "status": "applied",
        "body_sha256": plan["replacement_body_digest"],
        "invariants_verified": {
            "title": True,
            "base_ref": True,
            "base_sha": True,
            "head_sha": True,
            "draft": True,
            "body_matches": True,
        },
        "applied_at": _iso(plan["applied_at"]),
    }
