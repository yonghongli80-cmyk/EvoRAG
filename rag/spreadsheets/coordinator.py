"""Orchestrate safe spreadsheet planning and execution."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rag.spreadsheets.catalog import build_authorized_catalog
from rag.spreadsheets.planner import Clarification, QueryIntent, plan_spreadsheet_query
from rag.spreadsheets.query import (
    QueryScope,
    StructuredCitation,
    StructuredQueryInputError,
    execute_multi_dataset_query,
)

logger = logging.getLogger(__name__)

# One readable question per input-shaped failure code the executor can raise.
INPUT_CLARIFICATIONS: dict[str, str] = {
    "non_numeric_metric": "该字段不是数值字段，无法求和或平均；请改问数量、最大值、最小值，或指定一个数值字段。",
    "unknown_field": "问题中的字段无法在当前表格中唯一定位，请补充要统计的字段或表格。",
    "invalid_field_reference": "问题引用了当前表格中不存在的字段，请重新描述要统计的字段。",
    "conflicting_sheet_scope": "问题同时指向多个表格，请分别询问单个表格中的字段。",
    "cross_sheet_unsupported": "暂不支持跨表格统计，请分别询问单个表格中的字段。",
    "invalid_filter": "筛选条件不完整，请补充筛选值的范围。",
    "invalid_query_plan": "问题缺少要统计的字段，请说明要查询的内容。",
}


@dataclass(frozen=True)
class ComputedFact:
    label: str
    value: str


@dataclass(frozen=True)
class SpreadsheetQueryOutcome:
    kind: Literal[
        "not_applicable", "structured_result", "hybrid_result", "clarification_required",
        "unsupported", "failed",
    ]
    computed_facts: tuple[ComputedFact, ...] = ()
    citations: tuple[StructuredCitation, ...] = ()
    rows_scanned: int = 0
    clarification: Clarification | None = None
    # Diagnosable reason of a ``failed`` outcome; empty otherwise.
    error_code: str = ""


def _failure_code(exc: BaseException) -> str:
    """Return the stable audit code of an internal execution failure."""
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, sqlite3.Error):
        return "dataset_read_failed"
    return "execution_failed"


class SpreadsheetQueryCoordinator:
    def __init__(self, db, datasets_root: Path):
        self.db = db
        self.datasets_root = Path(datasets_root)
        self._cache: OrderedDict[tuple, SpreadsheetQueryOutcome] = OrderedDict()

    def has_datasets(self, kb_id: str, user: dict | None = None) -> bool:
        if self.db is None or kb_id is None:
            return False
        if user is None:
            user = {"is_admin": True}
        catalog = build_authorized_catalog(self.db, self.datasets_root, kb_id, user)
        return bool(catalog.datasets)

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
        dataset_ids = planning.plan.selected_dataset_ids()
        try:
            result = execute_multi_dataset_query(planning.plan, QueryScope(paths, frozenset(paths)))
        except StructuredQueryInputError as exc:
            # The plan itself is unusable (unknown field, cross-sheet reference,
            # an aggregation the column type cannot support): the user can fix
            # this by rephrasing, so ask instead of reporting a failure.
            question_text = INPUT_CLARIFICATIONS.get(exc.code, "请补充更明确的表格查询条件。")
            clarification = Clarification(code=exc.code, question=question_text)
            self._audit(user, dataset_ids, planning.plan.model_dump(), "clarification_required", exc.code, 0, started)
            return self._remember(cache_key, SpreadsheetQueryOutcome("clarification_required", clarification=clarification))
        except Exception as exc:
            # Anything else is an internal failure: never let it escape to the
            # API as a 500, and never pretend a value was computed.
            error_code = _failure_code(exc)
            logger.warning(
                "structured query failed (%s, %s): %s", error_code, type(exc).__name__, exc
            )
            self._audit(user, dataset_ids, planning.plan.model_dump(), "failed", error_code, 0, started)
            return self._remember(cache_key, SpreadsheetQueryOutcome("failed", error_code=error_code))
        facts = tuple(ComputedFact(f"{value.metric.column_key} {value.metric.function}", value.canonical) for value in result.values)
        if result.rows:
            facts = tuple(
                ComputedFact(
                    "，".join(f"{key}={value}" for key, value in list(row.values.items())[:-1]),
                    str(list(row.values.values())[-1]),
                )
                for row in result.rows
            )
        kind = "hybrid_result" if planning.intent == QueryIntent.HYBRID else "structured_result"
        self._audit(user, dataset_ids, planning.plan.model_dump(), "success", "", result.rows_scanned, started)
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
