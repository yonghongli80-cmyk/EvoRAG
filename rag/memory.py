"""Multi-turn dialogue memory with SQLite persistence and auto-summary."""

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

from config import settings
from rag.models import Chunk


@dataclass
class Message:
    role: str
    content: str


MAX_ROUNDS = 10

SYSTEM_PROMPT = (
    "你是一个有帮助的助手。请基于提供的上下文回答问题。"
    "如果上下文中没有答案，就说你不知道。"
    "保留原文中的专有名词、公司名称、技术术语、项目名称、人名不翻译，保持原始语言。"
    "例如：Hugging Face 不要翻译为拥抱脸，Ollama 不要翻译为奥拉马，LangChain 不要翻译为链式模型。"
    "\n回答时标注信息来源。使用 [序号] 格式引用，如："
    '"根据文档[1]，服务间调用超时默认为 3 秒[2]。"'
    "来源列表会在你收到的上下文中以 [1] 文件名(第N段) 的格式提供。"
    "如果答案无法从上下文中得出，明确说明'文档中未找到相关信息'，不要编造来源。"
)


class DialogueMemory:
    def __init__(
        self,
        db_path: str = settings.memory_db_path,
        generate_fn: Callable[[list[dict]], str] | None = None,
    ) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._generate_fn = generate_fn
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                summary TEXT DEFAULT '',
                last_summarized_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migration: add last_summarized_count to existing sessions tables
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN last_summarized_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)",
                (session_id,),
            )
            self._conn.commit()

    def get_recent_messages(self, session_id: str, n: int = 10) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        return [Message(role=r[0], content=r[1]) for r in reversed(rows)]

    def get_summary(self, session_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def save_summary(self, session_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, summary, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "summary = excluded.summary, updated_at = datetime('now')",
                (session_id, summary),
            )
            self._conn.commit()

    def should_summarize(self, session_id: str, max_rounds: int = MAX_ROUNDS) -> bool:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()[0]
            last = self._conn.execute(
                "SELECT last_summarized_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        last_count = last[0] if last else 0
        return total > max_rounds and total > last_count

    def summarize_old_rounds(self, session_id: str) -> None:
        if self._generate_fn is None:
            return
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()[0]
            if total <= MAX_ROUNDS:
                return
            last = self._conn.execute(
                "SELECT last_summarized_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            already_summarized = last[0] if last else 0
            # Keep the most recent MAX_ROUNDS user turns.  A round may contain
            # multiple assistant/tool messages, so derive the message boundary
            # from the first retained user-message ID rather than multiplying by 2.
            first_retained = self._conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = 'user' "
                "ORDER BY id ASC LIMIT 1 OFFSET ?",
                (session_id, total - MAX_ROUNDS),
            ).fetchone()
            if first_retained is None:
                return
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? AND id < ? ORDER BY id ASC",
                (session_id, first_retained[0]),
            ).fetchall()
        if not rows:
            return
        old_text = "\n".join(f"{r[0]}: {r[1]}" for r in rows)
        summary_msgs = [
            {"role": "system", "content": "总结以下对话为一段简短摘要（中文）："},
            {"role": "user", "content": old_text},
        ]
        try:
            summary = self._generate_fn(summary_msgs)
        except Exception:
            return
        self.save_summary(session_id, summary)
        with self._lock:
            # 重新读取 total 避免 TOCTOU 竞态
            fresh_total = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()[0]
            self._conn.execute(
                "UPDATE sessions SET last_summarized_count = ? WHERE session_id = ?",
                (fresh_total, session_id),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()

    def build_messages(self, session_id: str, query: str, context: list) -> list[dict]:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        summary = self.get_summary(session_id)
        if summary:
            msgs.append({"role": "system", "content": f"对话历史摘要：{summary}"})
        recent = self.get_recent_messages(session_id, MAX_ROUNDS)
        for m in recent:
            msgs.append({"role": m.role, "content": m.content})
        # Format context with source annotations
        ctx_parts = []
        for i, c in enumerate(context):
            if isinstance(c, Chunk):
                ctx_parts.append(f"[{i + 1}] {c.doc_name}(第{c.chunk_index + 1}段): {c.text}")
            else:
                ctx_parts.append(f"[{i + 1}] {c}")
        ctx_str = "\n\n".join(ctx_parts)
        msgs.append(
            {
                "role": "user",
                "content": f"相关文档：\n{ctx_str}\n\n问题：{query}",
            }
        )
        return msgs
