"""Persistent SQLite memory: short-term, long-term, and task memory."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS context (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    cwd TEXT,
    verdict TEXT,
    exit_code INTEGER,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- preference|project|command|environment|solution|decision|fact
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    project TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending|done|failed
    project TEXT,
    detail TEXT,
    next_steps TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(title, body, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO memories_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
"""

MEMORY_KINDS = ("preference", "project", "command", "environment", "solution", "decision", "fact")


@dataclass
class Memory:
    id: int
    kind: str
    title: str
    body: str
    project: str | None
    uses: int
    created: float
    updated: float


@dataclass
class Task:
    id: int
    title: str
    status: str
    project: str | None
    detail: str | None
    next_steps: str | None
    created: float
    updated: float


def _fts_query(text: str) -> str:
    words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words[:12])


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------ status
    def status(self) -> str:
        try:
            n = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            return f"OK ({n} memories)"
        except sqlite3.Error as e:
            return f"ERROR: {e}"

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------- short-term
    def add_message(self, session: str, role: str, content: str) -> None:
        self.db.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)", (session, role, content, time.time()))
        self.db.commit()

    def recent_messages(self, session: str, limit: int = 12) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE session=? ORDER BY id DESC LIMIT ?", (session, limit)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def last_session(self) -> str | None:
        row = self.db.execute("SELECT session FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        return row["session"] if row else None

    def history(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT role, content, ts FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]

    def set_context(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO context(key,value,ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, value, time.time()),
        )
        self.db.commit()

    def get_context(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM context WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def log_command(self, command: str, cwd: str, verdict: str, exit_code: int | None) -> None:
        self.db.execute(
            "INSERT INTO commands(command,cwd,verdict,exit_code,ts) VALUES (?,?,?,?,?)",
            (command, cwd, verdict, exit_code, time.time()),
        )
        self.db.commit()

    def last_commands(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.db.execute("SELECT command, exit_code, ts FROM commands ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def frequent_commands(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT command, COUNT(*) AS n FROM commands WHERE exit_code=0 GROUP BY command ORDER BY n DESC, MAX(ts) DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # -------------------------------------------------------- long-term
    def add_memory(self, kind: str, title: str, body: str, project: str | None = None) -> int:
        kind = kind if kind in MEMORY_KINDS else "fact"
        title, body = title.strip()[:120], body.strip()[:1500]
        if not title or not body:
            raise ValueError("title and body are required")
        dup = self.db.execute("SELECT id FROM memories WHERE kind=? AND lower(title)=lower(?)", (kind, title)).fetchone()
        now = time.time()
        if dup:
            self.db.execute("UPDATE memories SET body=?, project=COALESCE(?,project), updated=? WHERE id=?", (body, project, now, dup["id"]))
            self.db.commit()
            return dup["id"]
        cur = self.db.execute(
            "INSERT INTO memories(kind,title,body,project,created,updated) VALUES (?,?,?,?,?,?)",
            (kind, title, body, project, now, now),
        )
        self.db.commit()
        return cur.lastrowid

    def update_memory(self, mem_id: int, title: str | None = None, body: str | None = None) -> bool:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
        if not row:
            return False
        self.db.execute(
            "UPDATE memories SET title=?, body=?, updated=? WHERE id=?",
            (title or row["title"], body or row["body"], time.time(), mem_id),
        )
        self.db.commit()
        return True

    def delete_memory(self, mem_id: int) -> bool:
        cur = self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self.db.commit()
        return cur.rowcount > 0

    def list_memories(self, kind: str | None = None, limit: int = 50) -> list[Memory]:
        if kind:
            rows = self.db.execute("SELECT * FROM memories WHERE kind=? ORDER BY updated DESC LIMIT ?", (kind, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM memories ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def search_memories(self, query: str, limit: int = 8) -> list[Memory]:
        q = _fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id=f.rowid WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = self.db.execute("SELECT * FROM memories WHERE title LIKE ? OR body LIKE ? LIMIT ?", (like, like, limit)).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def relevant_for(self, text: str, limit: int = 5) -> list[Memory]:
        """Retrieve memories to inject into the prompt; bumps their usage counter."""
        mems = self.search_memories(text, limit)
        if mems:
            ids = ",".join(str(m.id) for m in mems)
            self.db.execute(f"UPDATE memories SET uses = uses + 1 WHERE id IN ({ids})")
            self.db.commit()
        return mems

    def preferences(self, limit: int = 8) -> list[Memory]:
        return self.list_memories("preference", limit)

    def clear(self, what: str = "memories") -> int:
        tables = {"memories": "memories", "history": "messages", "tasks": "tasks", "commands": "commands"}
        if what == "all":
            n = 0
            for t in ("memories", "messages", "tasks", "commands", "context"):
                n += self.db.execute(f"DELETE FROM {t}").rowcount
            self.db.commit()
            return n
        if what not in tables:
            raise ValueError(f"unknown target '{what}' (use memories|history|tasks|commands|all)")
        n = self.db.execute(f"DELETE FROM {tables[what]}").rowcount
        self.db.commit()
        return n

    # ------------------------------------------------------------ tasks
    def add_task(self, title: str, status: str = "pending", project: str | None = None, detail: str | None = None, next_steps: str | None = None) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tasks(title,status,project,detail,next_steps,created,updated) VALUES (?,?,?,?,?,?,?)",
            (title.strip()[:200], status, project, detail, next_steps, now, now),
        )
        self.db.commit()
        return cur.lastrowid

    def update_task(self, task_id: int, **fields) -> bool:
        allowed = {"title", "status", "detail", "next_steps", "project"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return False
        sets["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        cur = self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*sets.values(), task_id))
        self.db.commit()
        return cur.rowcount > 0

    def list_tasks(self, status: str | None = None, limit: int = 30) -> list[Task]:
        if status:
            rows = self.db.execute("SELECT * FROM tasks WHERE status=? ORDER BY updated DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [Task(**dict(r)) for r in rows]

    def last_task(self) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks ORDER BY updated DESC LIMIT 1").fetchone()
        return Task(**dict(row)) if row else None

    def open_tasks(self) -> list[Task]:
        rows = self.db.execute("SELECT * FROM tasks WHERE status IN ('pending','failed') ORDER BY updated DESC LIMIT 20").fetchall()
        return [Task(**dict(r)) for r in rows]

    # ----------------------------------------------------------- export
    def export(self, path: Path) -> Path:
        data = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "memories": [dict(r) for r in self.db.execute("SELECT * FROM memories ORDER BY id")],
            "tasks": [dict(r) for r in self.db.execute("SELECT * FROM tasks ORDER BY id")],
            "context": [dict(r) for r in self.db.execute("SELECT * FROM context")],
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return path

    # ----------------------------------------------------- resume text
    def resume_summary(self) -> str:
        parts = []
        cwd = self.get_context("cwd")
        proj = self.get_context("project")
        if cwd:
            parts.append(f"Working directory: {cwd}")
        if proj:
            parts.append(f"Project: {proj}")
        t = self.last_task()
        if t:
            parts.append(f"Last task [{t.status}]: {t.title}" + (f"\n  Next: {t.next_steps}" if t.next_steps else ""))
        open_ = [x for x in self.open_tasks() if not t or x.id != t.id]
        if open_:
            parts.append("Open tasks: " + "; ".join(f"#{x.id} {x.title}" for x in open_[:5]))
        return "\n".join(parts) or "Nothing to resume yet."
