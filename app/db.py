"""SQLite: схема, соединения, инициализация."""

import sqlite3
import threading
from pathlib import Path

from .config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Один писатель за раз. Импорт и фоновый пересчёт идут из разных потоков,
# и без этого SQLite отдаёт «database is locked» на апгрейде транзакции.
write_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    path = Path(get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def ping() -> bool:
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False
