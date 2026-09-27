"""Memory: conversations, durable facts and task history in SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .llm import cosine, hashed_vector

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL,
    meta TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    kind TEXT DEFAULT 'fact',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    session TEXT DEFAULT '',
    steps TEXT DEFAULT '[]',
    result TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    session TEXT DEFAULT '',
    vector TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);
CREATE INDEX IF NOT EXISTS idx_vectors_session ON vectors(session);
"""


class Memory:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- messages ----------------------------------------------------------
    def add_message(self, session: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session, role, content, ts, meta) VALUES (?,?,?,?,?)",
                (session, role, content, time.time(), json.dumps(meta or {}, default=str)),
            )
            self._conn.commit()

    def history(self, session: str, limit: int = 40) -> List[Dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def search_messages(self, session: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        terms = [t for t in query.lower().split() if len(t) > 2][:8]
        if not terms:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, ts FROM messages WHERE session=? ORDER BY id DESC LIMIT 500",
                (session,),
            ).fetchall()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in rows:
            text = row["content"].lower()
            score = sum(text.count(term) for term in terms)
            if score:
                scored.append((score, {"role": row["role"], "content": row["content"],
                                       "ts": row["ts"]}))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for item in scored[:limit]]

    # -- facts -------------------------------------------------------------
    def remember(self, key: str, value: str, kind: str = "fact") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts (key, value, kind, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, value, kind, time.time()),
            )
            self._conn.commit()

    def recall_facts(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, kind, ts FROM facts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- tasks -------------------------------------------------------------
    def upsert_task(self, task: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id,title,goal,status,created,updated,session,steps,result,error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, goal=excluded.goal, "
                "status=excluded.status, updated=excluded.updated, steps=excluded.steps, "
                "result=excluded.result, error=excluded.error",
                (task["id"], task["title"], task["goal"], task["status"],
                 task.get("created", time.time()), time.time(), task.get("session", ""),
                 json.dumps(task.get("steps", []), default=str), task.get("result", ""),
                 task.get("error", "")),
            )
            self._conn.commit()

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["steps"] = json.loads(item.get("steps") or "[]")
            out.append(item)
        return out

    def task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["steps"] = json.loads(item.get("steps") or "[]")
        return item

    # -- retrieval ---------------------------------------------------------
    def index_session(self, session: str, texts: List[str]) -> None:
        with self._lock:
            for text in texts:
                self._conn.execute(
                    "INSERT INTO vectors (ref, session, vector, ts) VALUES (?,?,?,?)",
                    (text[:200], session, json.dumps(hashed_vector(text)), time.time()),
                )
            self._conn.commit()

    def retrieve(self, session: str, query: str, limit: int = 4) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ref, vector FROM vectors WHERE session=? ORDER BY id DESC LIMIT 400",
                (session,),
            ).fetchall()
        target = hashed_vector(query)
        scored = [(cosine(target, json.loads(row["vector"])), row["ref"]) for row in rows]
        scored = [item for item in scored if item[0] > 0.12]
        scored.sort(reverse=True)
        return [{"score": round(score, 3), "text": ref} for score, ref in scored[:limit]]

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cursor = self._conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    def stats(self) -> Dict[str, int]:
        with self._lock:
            def count(table: str) -> int:
                return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return {table: count(table) for table in ("messages", "facts", "tasks", "vectors")}
