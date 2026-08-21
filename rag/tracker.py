"""执行追踪日志 — 记录 Agent 调用链用于排查。"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime

from config import settings


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
            [
                {"tool": tc.tool_name, "input": tc.input, "output": tc.output, "ms": tc.duration_ms}
                for tc in trace.tool_calls
            ],
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
