"""Session store: bounded, owner-bound history; the oldest session yields at the cap."""
import pytest

from odytor.browser import SessionStore
from odytor.errors import ValidationError

BASE = {'mocks': [], 'viewport': {'width': 1, 'height': 1}, 'locale': 'en-US', 'timezone': 'UTC', 'color_scheme': 'light', 'trace': False}


class Clock:
    now = 1000.0

    def __call__(self):
        self.now += 1
        return self.now


def make(store, owner='alice'):
    return store.create(owner=owner, repo='o/r', workspace_id='ws-' + 'a' * 32, version=0, tree='b' * 40, base=BASE)


def test_the_oldest_idle_session_is_evicted_at_the_cap_instead_of_locking_the_caller_out(tmp_path):
    store = SessionStore(tmp_path, max_per_owner=3, clock=Clock())
    ids = [make(store)['session_id'] for _ in range(3)]
    fourth = make(store)
    assert fourth['evicted_sessions'] == [ids[0]]
    with pytest.raises(ValidationError):
        store.get(ids[0], 'alice')
    assert store.get(ids[1], 'alice') and store.get(fourth['session_id'], 'alice')
    assert make(store, owner='bob')['evicted_sessions'] == []  # quotas are per caller


def test_a_session_with_a_run_in_flight_is_never_evicted(tmp_path):
    store = SessionStore(tmp_path, max_per_owner=2, clock=Clock())
    first, second = make(store), make(store)
    store.add_pending(first, 'test-' + '1' * 32, [{'op': 'goto', 'path': '/'}])
    third = make(store)
    assert third['evicted_sessions'] == [second['session_id']] and store.get(first['session_id'], 'alice')['pending']
    store.add_pending(third, 'test-' + '2' * 32, [{'op': 'goto', 'path': '/'}])
    with pytest.raises(ValidationError, match='run in flight'):
        make(store)


def test_foreign_and_malformed_session_ids_are_indistinguishable(tmp_path):
    store = SessionStore(tmp_path, clock=Clock())
    record = make(store)
    for bad, owner in ((record['session_id'], 'mallory'), ('bs-' + '0' * 32, 'alice'), ('../x', 'alice')):
        with pytest.raises(ValidationError, match='Unknown session_id'):
            store.get(bad, owner)
