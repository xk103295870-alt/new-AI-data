"""SessionStore：SQLite 会话与最小化历史持久化。

职责：
- session/message 的创建、追加与读取；为多轮追问提供有限上下文窗口；
- 只持久化用户问题、最终回答与最小查询元数据（行数/截断/来源）；
  绝不持久化原始查询结果行、列值、凭据或完整 schema。

安全边界：
- session_id 由服务端生成并做格式白名单校验，非法 ID 一律视为不存在；
- SQL 全部参数化，不拼接用户输入；
- 单实例写操作串行化，避免并发追加交错；读取只取最近 N 条作为上下文。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol
from uuid import uuid4

from app.errors import SessionNotFound

_SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{32}$")


def is_valid_session_id(value: str) -> bool:
    """会话 ID 只接受服务端生成的固定格式；其余不查询数据库。"""
    return isinstance(value, str) and bool(_SESSION_ID_RE.fullmatch(value))


@dataclass(frozen=True)
class QueryInfo:
    """对外暴露的最小查询元数据；不含 SQL、列值或结果行。"""

    row_count: int
    truncated: bool
    source_id: str


@dataclass(frozen=True)
class SessionMessage:
    """单条会话消息；query_info 只挂在 assistant 消息上。"""

    role: Literal["user", "assistant"]
    content: str
    created_at: str
    query_info: QueryInfo | None = None


@dataclass(frozen=True)
class Session:
    """会话快照；messages 已按上下文窗口截取并按时间升序。"""

    id: str
    source_id: str
    created_at: str
    updated_at: str
    messages: tuple[SessionMessage, ...] = ()


class SessionStore(Protocol):
    """可替换的会话存储协议。"""

    async def create(self, source_id: str) -> Session: ...

    async def get(self, session_id: str) -> Session | None: ...

    async def append_turn(
        self,
        session_id: str,
        *,
        user_content: str,
        answer: str,
        query_info: QueryInfo | None,
    ) -> None: ...


class SQLiteSessionStore:
    """基于标准库 sqlite3 的会话存储；写操作经 asyncio.Lock 串行化。"""

    def __init__(
        self,
        path: str | Path,
        *,
        max_context_messages: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_context_messages < 1:
            raise ValueError("invalid max_context_messages")
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._max_context_messages = max_context_messages
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._write_lock = asyncio.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    query_metadata TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages (session_id, id);
                """
            )

    def _now(self) -> str:
        return self._clock().isoformat()

    async def create(self, source_id: str) -> Session:
        """创建空会话并返回快照；source_id 由调用方先经注册表校验。"""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("invalid source_id")
        session_id = f"sess_{uuid4().hex}"
        now = self._now()

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO sessions (id, source_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, source_id, now, now),
                )

        await asyncio.to_thread(_write)
        return Session(
            id=session_id,
            source_id=source_id,
            created_at=now,
            updated_at=now,
        )

    async def get(self, session_id: str) -> Session | None:
        """读取会话与最近上下文；未知/非法 ID 返回 None，不区分原因。"""
        if not is_valid_session_id(session_id):
            return None

        def _read() -> Session | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, source_id, created_at, updated_at "
                    "FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    return None
                message_rows = conn.execute(
                    "SELECT role, content, query_metadata, created_at "
                    "FROM messages WHERE session_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (session_id, self._max_context_messages),
                ).fetchall()
            messages = tuple(
                SessionMessage(
                    role=msg["role"],
                    content=msg["content"],
                    created_at=msg["created_at"],
                    query_info=_parse_query_info(msg["query_metadata"]),
                )
                for msg in reversed(message_rows)
            )
            return Session(
                id=row["id"],
                source_id=row["source_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                messages=messages,
            )

        return await asyncio.to_thread(_read)

    async def append_turn(
        self,
        session_id: str,
        *,
        user_content: str,
        answer: str,
        query_info: QueryInfo | None,
    ) -> None:
        """原子追加一轮“用户问题 + 最终回答”；会话不存在时抛 SessionNotFound。"""
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(user_content, str) or not isinstance(answer, str):
            raise ValueError("invalid message content")
        now = self._now()
        metadata = (
            json.dumps(
                {
                    "row_count": query_info.row_count,
                    "truncated": query_info.truncated,
                    "source_id": query_info.source_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if query_info is not None
            else None
        )

        def _write() -> None:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists is None:
                    raise SessionNotFound()
                conn.execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, query_metadata, created_at) "
                    "VALUES (?, 'user', ?, NULL, ?)",
                    (session_id, user_content, now),
                )
                conn.execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, query_metadata, created_at) "
                    "VALUES (?, 'assistant', ?, ?, ?)",
                    (session_id, answer, metadata, now),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )

        async with self._write_lock:
            await asyncio.to_thread(_write)


def _parse_query_info(raw: str | None) -> QueryInfo | None:
    """解析持久化的最小元数据；损坏/未知结构返回 None，不伪装成有效结果。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    row_count = data.get("row_count")
    truncated = data.get("truncated")
    source_id = data.get("source_id")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 0
        or not isinstance(truncated, bool)
        or not isinstance(source_id, str)
    ):
        return None
    return QueryInfo(row_count=row_count, truncated=truncated, source_id=source_id)
