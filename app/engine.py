"""The single read path: generation cache → semantic layer → freshness."""

from __future__ import annotations

from typing import Any

from app.cache import GenerationCache
from app.freshness import freshness_for, generations
from app.semantic import Query, run
from app.warehouse import Warehouse


class Engine:
    def __init__(self, warehouse: Warehouse, cache: GenerationCache) -> None:
        self.warehouse = warehouse
        self.cache = cache

    def execute(self, query: Query) -> tuple[dict[str, Any], dict[str, Any]]:
        gens = generations(self.warehouse, query.sources)
        key = GenerationCache.key("query", query.canonical(), gens)
        cached = self.cache.get(key)
        hit = cached is not None
        if cached is None:
            cached = {"result": run(self.warehouse, query), "freshness": freshness_for(self.warehouse, query)}
            self.cache.put(key, cached)
        meta = {"freshness": cached["freshness"], "cache": {"hit": hit, "generations": gens}}
        return cached["result"], meta
