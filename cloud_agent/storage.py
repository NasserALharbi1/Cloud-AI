"""SQLite storage layer for memory, daily model usage counters, tasks, notes, and reminders."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cloud_agent.config import DB_PATH


class CloudStorage:
    """Thread-safe SQLite database manager living at %USERPROFILE%\\.cloud\\memory.db."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    date_str TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date_str, provider, model)
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_sensitive INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_key TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    hour_of_day INTEGER NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    suggested INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    due_date TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    trigger_time TEXT NOT NULL,
                    recurrence TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    # ── Daily Request Counters ────────────────────────────────────────────────

    def increment_model_request(self, provider: str, model: str) -> int:
        today = date.today().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_usage (date_str, provider, model, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(date_str, provider, model)
                DO UPDATE SET request_count = request_count + 1
                """,
                (today, provider, model),
            )
            conn.commit()
            row = conn.execute(
                "SELECT request_count FROM daily_usage WHERE date_str = ? AND provider = ? AND model = ?",
                (today, provider, model),
            ).fetchone()
            return int(row["request_count"]) if row else 1

    def get_requests_today_total(self) -> int:
        today = date.today().isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(request_count), 0) AS total FROM daily_usage WHERE date_str = ?",
                (today,),
            ).fetchone()
            return int(row["total"]) if row else 0

    def get_requests_today_by_model(self) -> Dict[str, int]:
        today = date.today().isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, model, request_count FROM daily_usage WHERE date_str = ?",
                (today,),
            ).fetchall()
            return {f"{r['provider']}/{r['model']}": int(r["request_count"]) for r in rows}

    # ── Memories (Self-learning) ──────────────────────────────────────────────

    def add_memory(self, category: str, content: str, is_sensitive: bool = False) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        content_clean = content.strip()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM memories WHERE LOWER(content) = LOWER(?)",
                (content_clean,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET category = ?, updated_at = ? WHERE id = ?",
                    (category, now, existing["id"]),
                )
                conn.commit()
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO memories (category, content, is_sensitive, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (category, content_clean, 1 if is_sensitive else 0, now, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_memories(self, category: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def search_memories(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            words = [w.strip() for w in query.split() if len(w.strip()) >= 2]
            if not words:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            clauses = " OR ".join(["content LIKE ?" for _ in words[:6]])
            params: List[Any] = [f"%{w}%" for w in words[:6]]
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {clauses} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (min(limit, 5),),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_memory(self, target: str) -> int:
        """Delete memory by ID or substring match. Returns count of deleted rows."""
        target = target.strip()
        with self._lock, self._connect() as conn:
            if target.lower() == "all":
                cur = conn.execute("DELETE FROM memories")
            elif target.isdigit():
                cur = conn.execute("DELETE FROM memories WHERE id = ?", (int(target),))
            else:
                cur = conn.execute("DELETE FROM memories WHERE content LIKE ?", (f"%{target}%",))
            conn.commit()
            return cur.rowcount

    # ── Proactive Pattern Tracking ────────────────────────────────────────────

    def record_action_pattern(self, pattern_key: str, description: str, hour: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Increment occurrence count for a recurring user workflow and return if threshold reached."""
        now_dt = datetime.now()
        hr = hour if hour is not None else now_dt.hour
        now_str = now_dt.isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_patterns WHERE pattern_key = ?",
                (pattern_key,),
            ).fetchone()
            if row:
                new_count = int(row["occurrences"]) + 1
                conn.execute(
                    "UPDATE action_patterns SET occurrences = ?, last_seen = ?, hour_of_day = ? WHERE id = ?",
                    (new_count, now_str, hr, row["id"]),
                )
                conn.commit()
                if new_count >= 3 and int(row["suggested"]) == 0:
                    conn.execute("UPDATE action_patterns SET suggested = 1 WHERE id = ?", (row["id"],))
                    conn.commit()
                    return {
                        "id": row["id"],
                        "pattern_key": pattern_key,
                        "description": description,
                        "hour_of_day": hr,
                        "occurrences": new_count,
                    }
            else:
                conn.execute(
                    """
                    INSERT INTO action_patterns (pattern_key, description, hour_of_day, occurrences, suggested, last_seen)
                    VALUES (?, ?, ?, 1, 0, ?)
                    """,
                    (pattern_key, description, hr, now_str),
                )
                conn.commit()
        return None

    # ── Tasks (Personal Assistant) ────────────────────────────────────────────

    def add_task(self, title: str, priority: str = "normal", due_date: str = "") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, status, priority, due_date, created_at) VALUES (?, 'pending', ?, ?, ?)",
                (title.strip(), priority, due_date, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_tasks(self, include_completed: bool = True) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if include_completed:
                rows = conn.execute("SELECT * FROM tasks ORDER BY status DESC, id ASC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM tasks WHERE status != 'done' ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def complete_task(self, task_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
            conn.commit()
            return cur.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Notes (Personal Assistant) ────────────────────────────────────────────

    def add_note(self, title: str, content: str, tags: str = "") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notes (title, content, tags, created_at) VALUES (?, ?, ?, ?)",
                (title.strip(), content.strip(), tags.strip(), now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_notes(self, query: str = "") -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if query:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? ORDER BY id DESC",
                    (f"%{query}%", f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    # ── Reminders ─────────────────────────────────────────────────────────────

    def add_reminder(self, message: str, trigger_time: str, recurrence: str = "") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (message, trigger_time, recurrence, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (message.strip(), trigger_time, recurrence, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_reminders(self, active_only: bool = True) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status = 'active' ORDER BY trigger_time ASC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM reminders ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def update_reminder_time(self, reminder_id: int, new_trigger_time: str, status: str = "active") -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE reminders SET trigger_time = ?, status = ? WHERE id = ?",
                (new_trigger_time, status, reminder_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_reminder_done(self, reminder_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE reminders SET status = 'fired' WHERE id = ?", (reminder_id,))
            conn.commit()
            return cur.rowcount > 0

    def delete_reminder(self, reminder_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            conn.commit()
            return cur.rowcount > 0
