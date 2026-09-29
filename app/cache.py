"""Generation-keyed result cache.

Invalidation by deleting keys needs to know every key a sync affected, which
means scanning. Instead, each source has a generation number that a
successful sync increments, and the generations of the sources a query
reads are part of its key. After a sync the old entries are simply
unreachable and age out; nothing scans, and a sync to `ad_spend` does not
evict cached revenue-only queries.

Authorization happens *before* the cache. The key holds no user identity
because the result does not depend on who asked, only on whether they may.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0


class GenerationCache:
    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.stats = CacheStats()

    @staticmethod
    def key(namespace: str, request: dict[str, Any], generations: dict[str, int]) -> str:
        payload = json.dumps(
            {"ns": namespace, "q": request, "gen": generations},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str, *, now: float | None = None) -> Any | None:
        moment = time.time() if now is None else now
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] <= moment:
                if item is not None:
                    del self._items[key]
                self.stats.misses += 1
                return None
            self._items.move_to_end(key)
            self.stats.hits += 1
            return item[1]

    def put(self, key: str, value: Any, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._lock:
            self._items[key] = (moment + self._ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.stats = CacheStats()
