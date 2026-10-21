"""执行追踪日志 — 记录 Agent 调用链用于排查。"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime

from config import settings

# Only sanitized decision metadata may reach the trace: model, question-set version, route,
# confidence, failure kind, attempt count, and durations. Never question text, chunk text, or keys.
_DECISION_METADATA_KEYS = frozenset(
    {
        # 用户点踩那条观测的溯源与去重键（实测：不在白名单里会被静默丢掉，去重就形同虚设）。
        "feedback_message_id",
        "jev_mode",
        "jev_model",
        "question_set_version",
        "passages_question_set_version",
        "jev_route",
        "legacy_route",
        "route_reason",
        "intent_confidence",
        "rewrite_probability",
        "failure_kind",
        "attempt_count",
        "rewrite_count",
        # 判定层原始统计量：候选数，以及判为相关/含答案证据/前提冲突/注入的块数。
        # 不含任何块文本或问题原文，只用于把失败归因到召回侧还是判定侧。
        "passage_judged",
        "passage_relevant",
        "passage_evidence",
        "passage_contradicted",
        "passage_injected",
        "jev_preflight_ms",
        "jev_passages_ms",
        "preparation_ms",
        # 结构化尝试的结果种类（not_applicable / structured_result / clarification_required /
        # unsupported / failed）。只是枚举值，不含问题或表格内容。
        "structured_query_kind",
        # 这条请求是否用了"原问题 + 改写句"双路重排。布尔值，用于判断改写抖动修复是否生效。
        "rewrite_dual_rerank",
    }
)


@dataclass
class ToolCall:
    tool_name: str
    input: str
    output: str
    duration_ms: float


@dataclass
class ExecutionTrace:
    question: str
    route: str
    answer: str = ""
    total_ms: float = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    chunk_hashes: list[str] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    decision_metadata: dict[str, object] = field(default_factory=dict)


def _sanitize_decision_metadata(metadata: dict) -> dict:
    """Keep allowlisted scalar keys only; anything else is dropped before persistence."""
    sanitized = {}
    for key, value in metadata.items():
        if key not in _DECISION_METADATA_KEYS:
            continue
        if isinstance(value, bool | int | float | str):
            sanitized[key] = value
    return sanitized


def _sanitize_stage_timings(stage_timings: dict) -> dict[str, float]:
    sanitized = {}
    for key, value in stage_timings.items():
        if key in _DECISION_METADATA_KEYS and isinstance(value, bool | int | float):
            sanitized[key] = float(value)
    return sanitized


class ExecutionTracker:
    def __init__(self, db_path: str | None = None):
        self._conn = sqlite3.connect(db_path or settings.memory_db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self):
        with self._lock:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS execution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                question TEXT NOT NULL,
                route TEXT NOT NULL,
                answer TEXT,
                total_ms REAL,
                details TEXT
            )""")
            self._conn.commit()

    def save(self, trace: ExecutionTrace):
        details = json.dumps(
            {
                "tool_calls": [
                    {"tool": tc.tool_name, "input": tc.input, "output": tc.output, "ms": tc.duration_ms}
                    for tc in trace.tool_calls
                ],
                "stage_timings": _sanitize_stage_timings(trace.stage_timings),
                "decision_metadata": _sanitize_decision_metadata(trace.decision_metadata),
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO execution_logs (timestamp, question, route, answer, total_ms, details) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), trace.question, trace.route, trace.answer, trace.total_ms, details),
            )
            self._conn.commit()

    def get_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
