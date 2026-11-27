from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from threading import Lock
from time import monotonic

_MISSING = object()


class TTLCache:
    """Bounded process-local cache; concurrent misses for one key share a load."""

    def __init__(self, max_entries=256, clock=monotonic):
        if max_entries < 1:
            raise ValueError('max_entries must be positive')
        self.max_entries = max_entries
        self.clock = clock
        self._entries = OrderedDict()
        self._loading = {}
        self._lock = Lock()

    def _read(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return _MISSING
        expires_at, value = entry
        if self.clock() >= expires_at:
            del self._entries[key]
            return _MISSING
        self._entries.move_to_end(key)
        return value

    def peek(self, key):
        with self._lock:
            value = self._read(key)
            return None if value is _MISSING else deepcopy(value)

    def get_or_load(self, key, ttl, loader):
        with self._lock:
            cached = self._read(key)
            if cached is not _MISSING:
                return deepcopy(cached)
            flight = self._loading.get(key)
            owner = flight is None
            if owner:
                flight = Future()
                self._loading[key] = flight
        if not owner:
            return deepcopy(flight.result())
        try:
            value = loader()
            snapshot = deepcopy(value)
        except BaseException as exc:
            with self._lock:
                del self._loading[key]
            # Existing waiters share the error; a new request may retry immediately.
            flight.set_exception(exc)
            raise
        with self._lock:
            self._entries[key] = (self.clock() + ttl, snapshot)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            del self._loading[key]
        flight.set_result(snapshot)
        return value
