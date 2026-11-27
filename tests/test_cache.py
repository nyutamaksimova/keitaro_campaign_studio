from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event

import pytest

from app.cache import TTLCache


def test_cache_expires_from_successful_load_and_copies_values():
    now = [0]
    cache = TTLCache(clock=lambda: now[0])
    calls = []

    def load():
        calls.append(True)
        now[0] += 5
        return [{'id': len(calls)}]

    first = cache.get_or_load('offers', 60, load)
    first[0]['id'] = 999
    now[0] = 64
    assert cache.get_or_load('offers', 60, load) == [{'id': 1}]
    now[0] = 65
    assert cache.get_or_load('offers', 60, load) == [{'id': 2}]
    assert len(calls) == 2


def test_failed_load_is_not_cached_and_expired_data_is_not_returned():
    now = [0]
    cache = TTLCache(clock=lambda: now[0])
    cache.get_or_load('offers', 60, lambda: ['old'])
    now[0] = 60

    def fail():
        raise ValueError('upstream failed')

    with pytest.raises(ValueError):
        cache.get_or_load('offers', 60, fail)
    assert cache.get_or_load('offers', 60, lambda: ['new']) == ['new']


def test_concurrent_requests_share_one_load():
    cache = TTLCache()
    entered, release = Event(), Event()
    calls = []

    def load():
        calls.append(True)
        entered.set()
        assert release.wait(timeout=3)
        return ['loaded']

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(cache.get_or_load, 'offers', 60, load) for _ in range(4)]
        assert entered.wait(timeout=3)
        release.set()
        assert [future.result(timeout=3) for future in futures] == [['loaded']] * 4
    assert len(calls) == 1


def test_cache_evicts_oldest_entries_when_full():
    cache = TTLCache(max_entries=2)
    cache.get_or_load('first', 60, lambda: 1)
    cache.get_or_load('second', 60, lambda: 2)
    cache.get_or_load('third', 60, lambda: 3)
    assert cache.peek('first') is None
    assert cache.peek('second') == 2


def test_concurrent_failed_load_is_shared_but_next_request_can_retry(monkeypatch):
    import app.cache

    joined = Barrier(4)

    class WaitingFuture(Future):
        def result(self, timeout=None):
            joined.wait(timeout=2)
            return super().result(timeout=timeout)

    # Synchronize waiters at the actual wait boundary rather than sleeping.
    monkeypatch.setattr(app.cache, 'Future', WaitingFuture, raising=False)
    cache = TTLCache()
    calls = []

    def fail():
        calls.append(True)
        try:
            joined.wait(timeout=2)
        except BrokenBarrierError:
            pass
        raise ValueError('upstream failed')

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(cache.get_or_load, 'offers', 60, fail) for _ in range(4)]
        for future in futures:
            with pytest.raises(ValueError, match='upstream failed'):
                future.result(timeout=4)
    assert len(calls) == 1
    assert cache.get_or_load('offers', 60, lambda: ['recovered']) == ['recovered']
