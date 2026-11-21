"""Campaign lifecycle: durable cancel, restart reconciliation, atomic digest-bound idempotency, retention."""
import threading
import time

import pytest

from odytor.campaign import ACTIVE, CampaignRecord, CampaignStopped, request_digest, run_validation_campaign
from odytor.errors import ValidationError
from test_campaign import OWNER, test_env  # noqa: F401  (fixture)
from test_git_workspace import client_for, local_fetch, repository, run  # noqa: F401

CID = "cmp-" + "a" * 32


def _record(cid=CID, **kw):
    now = time.time()
    base = dict(campaign_id=cid, owner=OWNER, repo="owner/repo", number=1, mode="pr_head", status="running", created_at=now, updated_at=now)
    base.update(kw)
    return CampaignRecord(**base)


def _service(test_env, instance="inst-1", live=("inst-1",)):
    svc = test_env["job_service"]
    svc.instance = instance
    svc.live_instances.return_value = set(live)
    return svc


def _run(test_env, svc, **kw):
    args = dict(expected_head=test_env["head"], mode="pr_head", profiles=["python-pytest"], workspaces=test_env["ws_service"], service=svc,
                sessions=None, policies=test_env["policies"], owner=OWNER, cfg=test_env["cfg"], campaign_store=test_env["store"], campaign_id=CID)
    args.update(kw)
    return run_validation_campaign(test_env["client"], "owner/repo", 1, **args)


def test_workspace_and_child_run_identity_are_persisted(test_env):
    store = test_env["store"]
    store.save(_record(workspace_id="ws-" + "2" * 32, current_run_id="test-" + "3" * 32, instance="inst-1"))
    loaded = store.load(CID, OWNER)
    assert loaded.workspace_id == "ws-" + "2" * 32 and loaded.current_run_id == "test-" + "3" * 32 and loaded.instance == "inst-1"


def test_cancel_mid_run_is_durable_and_releases_everything(test_env, monkeypatch):
    store, ws, svc = test_env["store"], test_env["ws_service"], _service(test_env)
    release, canceled_children = threading.Event(), []
    monkeypatch.setattr("odytor.campaign.cancel_test_run", lambda run_id, **kw: canceled_children.append(run_id))
    monkeypatch.setattr("odytor.campaign.submit_workspace_run", lambda *a, **k: {"run_id": "test-" + "0" * 32})
    monkeypatch.setattr("odytor.campaign.wait_for_run", lambda s, r, t: (release.wait(20), {"status": "canceled"})[1])
    store.save(_record(instance="inst-1"))
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=_run(test_env, svc)))
    worker.start()
    for _ in range(200):
        rec = store.load(CID, OWNER)
        if rec and rec.current_run_id:
            break
        time.sleep(0.02)
    assert rec.current_run_id == "test-" + "0" * 32 and rec.workspace_id

    res = store.cancel(CID, OWNER, service=svc, workspaces=ws)
    assert res["status"] == "canceling" and res["cleanup"] == "pending" and res["child_run_id"] == "test-" + "0" * 32
    assert canceled_children == ["test-" + "0" * 32]
    release.set()
    worker.join(20)

    final = store.load(CID, OWNER)
    assert final.status == "canceled", "the worker must never turn a canceled campaign into passed or failed"
    assert final.workspace_cleaned is True and ws.peek(rec.workspace_id, OWNER) is None
    assert final.failure["kind"] == "canceled" and final.current_run_id is None
    assert store.cancel(CID, OWNER, service=svc, workspaces=ws)["already_finished"] is True


def test_cancel_between_steps_starts_no_child_run(test_env, monkeypatch):
    store, svc = test_env["store"], _service(test_env)
    started = []
    monkeypatch.setattr("odytor.campaign.submit_workspace_run", lambda *a, **k: started.append(k["profile"]) or {"run_id": "test-" + "1" * 32})
    monkeypatch.setattr("odytor.campaign.wait_for_run", lambda s, r, t: {"status": "passed", "counts": {}})
    real_apply = test_env["ws_service"].apply_patch
    store.save(_record(instance="inst-1"))
    # cancel lands right after the workspace exists, before the first profile
    orig_open = __import__("odytor.campaign", fromlist=["open_workspace"]).open_workspace

    def open_then_cancel(*a, **k):
        ws = orig_open(*a, **k)
        store.cancel(CID, OWNER, service=svc, workspaces=test_env["ws_service"])
        return ws

    monkeypatch.setattr("odytor.campaign.open_workspace", open_then_cancel)
    _run(test_env, svc)
    assert started == [] and store.load(CID, OWNER).status == "canceled" and store.load(CID, OWNER).workspace_cleaned
    assert real_apply  # silence unused


def test_cancel_of_an_orphaned_campaign_finishes_immediately(test_env):
    store, ws = test_env["store"], test_env["ws_service"]
    svc = _service(test_env, instance="new-process", live=("new-process",))
    handle = ws.create(owner=OWNER, repo="owner/repo", source={"kind": "branch"}, populate=lambda r: _seed(r), pr=1)
    store.save(_record(workspace_id=handle["workspace_id"], instance="dead-process"))
    res = store.cancel(CID, OWNER, service=svc, workspaces=ws)
    assert res["status"] == "canceled" and res["workspace_cleaned"] is True and ws.peek(handle["workspace_id"], OWNER) is None


def _seed(repository):
    repository.git("config", "user.email", "a@b.c")
    blob = repository.git("hash-object", "-w", "--stdin", input=b"x\n").decode().strip()
    repository.git("update-index", "--add", "--cacheinfo", f"100644,{blob},f.txt")
    return repository.git("write-tree").decode().strip()


def test_restart_reconciles_unfinished_campaigns_and_same_key_retry_is_not_stuck(test_env):
    store, ws = test_env["store"], test_env["ws_service"]
    svc = _service(test_env, instance="new-process", live=("new-process",))
    handle = ws.create(owner=OWNER, repo="owner/repo", source={"kind": "branch"}, populate=_seed, pr=1)
    rec = _record(workspace_id=handle["workspace_id"], instance="dead-process", idempotency_key="k1", request_digest="d")
    store.claim(rec)
    assert store.find_by_idempotency_key(OWNER, "owner/repo", "k1").status == "running"

    out = store.maintain(service=svc, workspaces=ws, force=True)
    assert out["reconciled"] == 1
    done = store.find_by_idempotency_key(OWNER, "owner/repo", "k1")
    assert done.status == "setup_error" and done.failure["kind"] == "service_restarted" and done.workspace_cleaned
    assert ws.peek(handle["workspace_id"], OWNER) is None
    assert "new idempotency_key" in done.failure["message"]
    # a live sibling's campaign is never touched
    store.save(_record("cmp-" + "b" * 32, instance="new-process"))
    store.maintain(service=svc, workspaces=ws, force=True)
    assert store.load("cmp-" + "b" * 32, OWNER).status == "running"


def test_terminal_records_are_never_overwritten_by_a_stale_worker(test_env):
    store = test_env["store"]
    store.save(_record(status="canceled", finished_at=time.time()))
    stale = _record(status="running")
    with pytest.raises(CampaignStopped):
        store.checkpoint(stale)
    assert store.finalize(stale).status == "canceled"
    assert store.update(CID, OWNER, lambda r: setattr(r, "status", "passed")).status == "canceled"


def test_identical_requests_replay_and_changed_content_is_rejected(test_env):
    store = test_env["store"]
    d1 = request_digest(repo="owner/repo", number=1, overlay_patch="A")
    first, created = store.claim(_record(idempotency_key="k", request_digest=d1))
    again, created2 = store.claim(_record("cmp-" + "c" * 32, idempotency_key="k", request_digest=d1))
    assert created and not created2 and again.campaign_id == first.campaign_id
    assert not (store.root / ("cmp-" + "c" * 32 + ".json")).exists()
    with pytest.raises(ValidationError) as exc:
        store.claim(_record("cmp-" + "d" * 32, idempotency_key="k", request_digest=request_digest(repo="owner/repo", number=1, overlay_patch="B")))
    assert exc.value.diagnostic["classification"] == "idempotency_key_conflict"
    # another owner or repository gets its own key space
    other, created3 = store.claim(_record("cmp-" + "e" * 32, owner="someone-else", idempotency_key="k", request_digest="x"))
    assert created3


def test_concurrent_identical_submissions_create_exactly_one_campaign(test_env):
    store, results, barrier = test_env["store"], [], threading.Barrier(8)

    def submit(i):
        barrier.wait()
        rec, created = store.claim(_record(f"cmp-{i:032x}", idempotency_key="race", request_digest="same"))
        results.append((rec.campaign_id, created))

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for _, created in results if created) == 1 and len({cid for cid, _ in results}) == 1
    assert len(list(store.root.glob("cmp-*.json"))) == 1


def test_campaign_deadline_cancels_the_child_run_it_owns(test_env, monkeypatch):
    svc, canceled = _service(test_env), []
    monkeypatch.setattr("odytor.campaign.cancel_test_run", lambda run_id, **kw: canceled.append(run_id))
    monkeypatch.setattr("odytor.campaign.submit_workspace_run", lambda *a, **k: {"run_id": "test-" + "4" * 32})
    outcomes = iter([None, {"status": "canceled"}])
    monkeypatch.setattr("odytor.campaign.wait_for_run", lambda s, r, t: next(outcomes))
    result = _run(test_env, svc)
    assert canceled == ["test-" + "4" * 32] and result["status"] == "timeout" and result["results"]["python-pytest"]["status"] == "timeout"
    assert result["workspace_cleaned"] is True


def test_retention_purges_old_terminal_records_and_releases_unusable_plan_workspaces(test_env):
    store, ws = test_env["store"], test_env["ws_service"]
    svc = _service(test_env)
    handle = ws.create(owner=OWNER, repo="owner/repo", source={"kind": "branch"}, populate=_seed, pr=1)
    old = time.time() - 3 * 24 * 3600
    store.claim(_record("cmp-" + "1" * 32, status="passed", finished_at=old, idempotency_key="old", request_digest="d", workspace_cleaned=True))
    store.save(_record("cmp-" + "2" * 32, status="passed", finished_at=time.time() - 3 * 3600, workspace_id=handle["workspace_id"],
                       push_plan={"plan_id": "pp-" + "9" * 32}, workspace_cleaned=False))
    store.save(_record("cmp-" + "3" * 32, status="passed", finished_at=time.time()))
    out = store.maintain(service=svc, workspaces=ws, plans=test_env["plans"], force=True)
    assert out["purged"] == 1 and out["released"] == 1
    assert not (store.root / ("cmp-" + "1" * 32 + ".json")).exists()
    assert list(store.root.glob("idx_*.id")) == []           # its idempotency claim went with it
    assert ws.peek(handle["workspace_id"], OWNER) is None
    assert (store.root / ("cmp-" + "3" * 32 + ".json")).exists()


def test_active_states_are_not_purged(test_env):
    store, svc = test_env["store"], _service(test_env)
    store.save(_record(instance="inst-1", created_at=time.time() - 10 * 24 * 3600))
    store.maintain(service=svc, workspaces=test_env["ws_service"], force=True)
    assert store.load(CID, OWNER).status in ACTIVE
