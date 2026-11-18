"""Dependency environment reuse, atomic preparation, locking, eviction and debris recovery."""
import os
import shutil
import threading
import time
from collections import namedtuple

import pytest

from git_plus import dep_cache
from git_plus.dep_cache import EnvIdentity, EnvPreparationError, ResourceUnavailable, acquire, collect

ID = EnvIdentity('Owner/Repo', 'a' * 64, '3.12.1', 'b' * 64, '1', 'linux/amd64')


def prep(payload=b'x' * 1000, delay=0.0, calls=None):
    def build(tmp):
        if calls is not None:
            calls.append(tmp.name)
        time.sleep(delay)
        (tmp / 'site' / 'mod.py').write_bytes(payload)
        return {'python': '3.12.1'}
    return build


def test_identity_changes_with_every_compatibility_input():
    keys = {ID.key()}
    for field, value in (('repo', 'other/repo'), ('requirements_sha256', 'c' * 64), ('python', '3.13.0'),
                         ('image_id', 'd' * 64), ('profile_version', '2'), ('platform', 'linux/arm64')):
        keys.add(EnvIdentity(**{**ID.__dict__, field: value}).key())
    assert len(keys) == 7
    assert EnvIdentity(**{**ID.__dict__, 'repo': 'owner/repo'}).key() == ID.key()  # repository names are case-insensitive


def test_first_use_misses_then_reuses_without_preparing(tmp_path):
    calls = []
    with acquire(tmp_path, ID, prep(calls=calls)) as first:
        assert first.cache_status == 'miss' and first.prepare_seconds >= 0 and (first.path / 'site' / 'mod.py').exists()
        assert not (tmp_path / 'envs' / f'.prep-{first.key}').exists()
    with acquire(tmp_path, ID, prep(calls=calls)) as second:
        assert second.cache_status == 'hit' and second.prepare_seconds == 0.0 and second.path == first.path
    assert len(calls) == 1


def test_changed_specification_creates_a_new_environment_and_keeps_the_old_one(tmp_path):
    other = EnvIdentity(**{**ID.__dict__, 'requirements_sha256': 'e' * 64})
    with acquire(tmp_path, ID, prep(b'old')) as a, acquire(tmp_path, other, prep(b'new')) as b:
        assert a.key != b.key and (a.path / 'site/mod.py').read_bytes() == b'old' and (b.path / 'site/mod.py').read_bytes() == b'new'


def test_failed_preparation_leaves_nothing_and_is_retried(tmp_path):
    def broken(tmp):
        (tmp / 'site' / 'half').write_text('partial')
        raise EnvPreparationError('pip failed', {'exit': 1})
    with pytest.raises(EnvPreparationError):
        with acquire(tmp_path, ID, broken):
            pytest.fail('must not yield')
    assert [p.name for p in (tmp_path / 'envs').iterdir() if p.is_dir()] == []
    with acquire(tmp_path, ID, prep()) as env:
        assert env.cache_status == 'miss'


def test_keyboard_interrupt_during_preparation_cleans_up(tmp_path):
    def interrupted(tmp):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        with acquire(tmp_path, ID, interrupted):
            pass
    assert not [p for p in (tmp_path / 'envs').iterdir() if p.is_dir()]


def test_incomplete_or_mismatched_environments_are_never_reused(tmp_path):
    envs = tmp_path / 'envs'
    final = envs / ID.key()
    (final / 'site').mkdir(parents=True)
    (final / 'site' / 'poison.py').write_text('evil')
    with acquire(tmp_path, ID, prep(b'good')) as env:  # no ready marker: rebuilt from scratch
        assert env.cache_status == 'miss' and not (env.path / 'site' / 'poison.py').exists()
    (final / dep_cache.READY).write_text('{"key": "someone-else"}')  # marker for a different identity
    with acquire(tmp_path, ID, prep(b'again')) as env:
        assert env.cache_status == 'miss' and (env.path / 'site/mod.py').read_bytes() == b'again'
    (final / dep_cache.READY).write_text('not json')
    with acquire(tmp_path, ID, prep(b'third')) as env:
        assert env.cache_status == 'miss'


def test_stale_partial_preparation_from_a_crashed_runner_is_replaced(tmp_path):
    stale = tmp_path / 'envs' / f'.prep-{ID.key()}'
    (stale / 'site').mkdir(parents=True)
    (stale / 'site' / 'garbage').write_text('crash leftovers')
    with acquire(tmp_path, ID, prep()) as env:
        assert env.cache_status == 'miss' and not (env.path / 'site' / 'garbage').exists()


def test_concurrent_requests_prepare_once(tmp_path):
    calls, results = [], []

    def worker():
        with acquire(tmp_path, ID, prep(delay=0.6, calls=calls)) as env:
            results.append(env.cache_status)
            time.sleep(0.1)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]; [t.join(30) for t in threads]
    assert len(calls) == 1 and sorted(results) == ['hit_after_wait'] * 5 + ['miss']


def test_environment_is_invisible_until_fully_built(tmp_path):
    seen = {}

    def build(tmp):
        seen['final_exists'] = (tmp_path / 'envs' / ID.key()).exists()
        (tmp / 'site' / 'x').write_text('x')
        return {}
    with acquire(tmp_path, ID, build):
        pass
    assert seen == {'final_exists': False}


def make_envs(tmp_path, sizes, now=None):
    """Create ready environments with fixed sizes; returns identities oldest first."""
    now = now or time.time()
    out = []
    for i, size in enumerate(sizes):
        ident = EnvIdentity(**{**ID.__dict__, 'requirements_sha256': f'{i:064x}'})
        with acquire(tmp_path, ident, prep(b'x' * size)):
            pass
        stamp = now - (len(sizes) - i) * 100
        os.utime(tmp_path / 'envs' / f'{ident.key()}.use', (stamp, stamp))
        out.append(ident)
    return out


def alive(tmp_path, idents):
    return [(tmp_path / 'envs' / i.key()).exists() for i in idents]


def test_ttl_evicts_only_unused_old_environments(tmp_path):
    ids = make_envs(tmp_path, [100, 100, 100])
    report = collect(tmp_path, ttl_seconds=150, budget_bytes=10**9, min_free_bytes=1)
    assert alive(tmp_path, ids) == [False, False, True]
    assert {e['reason'] for e in report['evicted']} == {'ttl'} and report['env_count_after'] == 1


def test_budget_eviction_is_least_recently_used_first(tmp_path):
    ids = make_envs(tmp_path, [4000, 4000, 4000])
    report = collect(tmp_path, ttl_seconds=10**9, budget_bytes=9000, min_free_bytes=1)
    assert alive(tmp_path, ids) == [False, True, True] and report['evicted'][0]['reason'] == 'disk_budget'
    assert report['env_bytes_after'] < report['env_bytes_before']


def test_using_an_environment_refreshes_its_recency(tmp_path):
    ids = make_envs(tmp_path, [4000, 4000, 4000])
    with acquire(tmp_path, ids[0], prep()):
        pass
    collect(tmp_path, ttl_seconds=10**9, budget_bytes=9000, min_free_bytes=1)
    assert alive(tmp_path, ids) == [True, False, True]


def test_active_environment_is_protected_from_eviction(tmp_path):
    ids = make_envs(tmp_path, [4000, 4000])
    with acquire(tmp_path, ids[0], prep()) as held:
        report = collect(tmp_path, ttl_seconds=0, budget_bytes=0, min_free_bytes=1)
        assert (held.path / 'site' / 'mod.py').exists() and ids[0].key() in report['protected']
        assert alive(tmp_path, ids) == [True, False]
    collect(tmp_path, ttl_seconds=0, budget_bytes=0, min_free_bytes=1)
    assert alive(tmp_path, ids) == [False, False]


def test_disk_pressure_evicts_before_admitting_and_rejects_when_it_cannot(tmp_path, monkeypatch):
    ids = make_envs(tmp_path, [4000, 4000])
    usage = namedtuple('usage', 'total used free')
    state = {'free': 100}

    def fake_usage(path):
        free = state['free'] + (10_000 * (2 - len(dep_cache.inventory(tmp_path))))  # eviction frees space
        return usage(10**9, 0, free)
    monkeypatch.setattr(shutil, 'disk_usage', fake_usage)
    report = collect(tmp_path, ttl_seconds=10**9, budget_bytes=10**9, min_free_bytes=5000)
    assert report['evicted'][0]['reason'] == 'free_space' and alive(tmp_path, ids) == [False, True]
    monkeypatch.setattr(shutil, 'disk_usage', lambda p: usage(10**9, 0, 10))
    with pytest.raises(ResourceUnavailable) as caught:
        collect(tmp_path, ttl_seconds=10**9, budget_bytes=10**9, min_free_bytes=5000)
    assert caught.value.detail['free_bytes_after'] == 10 and alive(tmp_path, ids) == [False, False]


def test_debris_from_crashes_is_swept_but_live_work_is_not(tmp_path):
    envs, jobs = tmp_path / 'envs', tmp_path / 'jobs'
    old = time.time() - 4000
    junk = [envs / '.evict-x-1', envs / f'.prep-{"f" * 40}', envs / ('1' * 40), jobs / 'dead-job']
    for path in junk:
        path.mkdir(parents=True)
        (path / 'f').write_text('x')
    (jobs / 'dead-job' / '.lock').write_text('')
    live = jobs / 'live-job'
    live.mkdir()
    fresh = envs / '.prep-fresh'
    fresh.mkdir()
    for path in [*junk, live]:
        os.utime(path, (old, old))
    fd = os.open(live / '.lock', os.O_RDWR | os.O_CREAT)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        report = collect(tmp_path, ttl_seconds=10**9, budget_bytes=10**9, min_free_bytes=1)
    finally:
        os.close(fd)
    assert report['debris_removed'] == 4
    assert not any(p.exists() for p in junk) and live.exists() and fresh.exists()


def test_cache_statistics_accumulate(tmp_path):
    dep_cache.bump_stats(tmp_path, hits=1)
    assert dep_cache.bump_stats(tmp_path, hits=2, misses=1) == {'hits': 3, 'misses': 1}


def test_the_environment_a_job_is_about_to_use_is_never_evicted_by_its_own_admission(tmp_path):
    ids = make_envs(tmp_path, [4000, 4000, 4000])
    report = collect(tmp_path, ttl_seconds=0, budget_bytes=0, min_free_bytes=1, keep=frozenset({ids[2].key()}))
    assert alive(tmp_path, ids) == [False, False, True] and len(report['evicted']) == 2
