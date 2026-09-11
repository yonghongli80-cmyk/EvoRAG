"""Orchestrate safe spreadsheet planning and execution."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rag.spreadsheets.catalog import build_authorized_catalog
from rag.spreadsheets.planner import Clarification, QueryIntent, plan_spreadsheet_query
from rag.spreadsheets.query import QueryScope, QueryValue, StructuredCitation, execute_multi_dataset_query


@dataclass(frozen=True)
class ComputedFact:
    label: str
    value: str


@dataclass(frozen=True)
class SpreadsheetQueryOutcome:
    kind: Literal["not_applicable", "structured_result", "hybrid_result", "clarification_required", "unsupported"]
    computed_facts: tuple[ComputedFact, ...] = ()
    citations: tuple[StructuredCitation, ...] = ()
    rows_scanned: int = 0
    clarification: Clarification | None = None


class SpreadsheetQueryCoordinator:
    def __init__(self, db, datasets_root: Path):
        self.db = db
        self.datasets_root = Path(datasets_root)
        self._cache: OrderedDict[tuple, SpreadsheetQueryOutcome] = OrderedDict()

    def query(self, question: str, *, kb_id: str, user: dict) -> SpreadsheetQueryOutcome:
        started = time.perf_counter()
        catalog = build_authorized_catalog(self.db, self.datasets_root, kb_id, user)
        cache_key = (
            kb_id, int(user.get("id", 0)), bool(user.get("is_admin")), question.strip().casefold(),
            tuple((item.dataset_id, item.version_id) for item in catalog.datasets),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached
        planning = plan_spreadsheet_query(question, catalog)
        if planning.intent == QueryIntent.SEMANTIC:
            return self._remember(cache_key, SpreadsheetQueryOutcome("not_applicable"))
        if planning.kind == "clarification_required":
            self._audit(user, (), None, "clarification_required", planning.clarification.code, 0, started)
            return self._remember(cache_key, SpreadsheetQueryOutcome("clarification_required", clarification=planning.clarification))
        if planning.kind != "planned" or planning.plan is None:
            return self._remember(cache_key, SpreadsheetQueryOutcome("unsupported"))
        paths = {item.dataset_id: item.path for item in catalog.datasets}
        result = execute_multi_dataset_query(planning.plan, QueryScope(paths, frozenset(paths)))
        facts = tuple(ComputedFact(f"{value.metric.column_key} {value.metric.function}", value.canonical) for value in result.values)
        kind = "hybrid_result" if planning.intent == QueryIntent.HYBRID else "structured_result"
        self._audit(user, planning.plan.selected_dataset_ids(), planning.plan.model_dump(), "success", "", result.rows_scanned, started)
        return self._remember(cache_key, SpreadsheetQueryOutcome(kind, facts, result.citations, result.rows_scanned))

    def _remember(self, key, outcome):
        self._cache[key] = outcome
        self._cache.move_to_end(key)
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        return outcome

    def _audit(self, user, dataset_ids, plan, status, error_code, rows_scanned, started):
        if hasattr(self.db, "record_structured_query_run"):
            self.db.record_structured_query_run(user.get("id"), dataset_ids, plan, status, error_code, rows_scanned, int((time.perf_counter() - started) * 1000))
