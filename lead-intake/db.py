"""SQLite storage for processed leads."""
import sqlite3
from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_leads (
            lead_id INTEGER PRIMARY KEY,
            source TEXT,
            title TEXT,
            qualified INTEGER,  -- 1=yes, 0=no
            category TEXT,      -- A+/A/B/C/spam
            summary TEXT,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def is_processed(lead_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM processed_leads WHERE lead_id=?", (lead_id,)).fetchone()
    conn.close()
    return row is not None


def save_result(lead_id: int, source: str, title: str, qualified: bool, category: str, summary: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO processed_leads (lead_id, source, title, qualified, category, summary) VALUES (?,?,?,?,?,?)",
        (lead_id, source, title, 1 if qualified else 0, category, summary),
    )
    conn.commit()
    conn.close()
