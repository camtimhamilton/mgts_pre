"""Immutable local demo snapshots. Inputs/model artifacts are not persisted."""
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from app.schemas import AnalysisResult


class SnapshotRepository(Protocol):
    def save(self, result: AnalysisResult) -> None: ...
    def get(self, analysis_id: str) -> AnalysisResult | None: ...


class SQLiteSnapshots:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS snapshots (analysis_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("PRAGMA user_version = 1")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def save(self, result: AnalysisResult) -> None:
        payload = result.model_dump_json()
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO snapshots (analysis_id, payload) VALUES (?, ?)",
                               (result.analysis_id, payload))
            stored = connection.execute("SELECT payload FROM snapshots WHERE analysis_id = ?", (result.analysis_id,)).fetchone()
            if stored[0] != payload:
                raise ValueError("immutable_snapshot_conflict")

    def get(self, analysis_id: str) -> AnalysisResult | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT payload FROM snapshots WHERE analysis_id = ?", (analysis_id,)).fetchone()
        return AnalysisResult.model_validate_json(row[0]) if row else None
