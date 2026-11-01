import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "agent.db"

class MemoryDB:
    def __init__(self, path=DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            notes TEXT DEFAULT '', updated_at TEXT NOT NULL)""")
        self.conn.commit()

    def add_message(self, role, content):
        self.conn.execute("INSERT INTO messages(role,content,created_at) VALUES(?,?,?)",
                          (role, content, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def recent_messages(self, limit=12):
        rows = self.conn.execute(
            "SELECT role,content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def add_task(self, title, notes=""):
        self.conn.execute("INSERT INTO tasks(title,status,notes,updated_at) VALUES(?,?,?,?)",
                          (title, "open", notes, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def recent_tasks(self, limit=10):
        return self.conn.execute(
            "SELECT id,title,status,notes,updated_at FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self):
        self.conn.close()
