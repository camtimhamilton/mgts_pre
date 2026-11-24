"""Работа с парами и анкетами. Единственное место, где это делается.

И генератор демо-данных, и импорт, и живой приём ходят сюда, поэтому правила
идемпотентности описаны один раз.
"""

from __future__ import annotations

import json
import sqlite3

from .normalize import Submission, content_hash


def get_or_create_pair(
    conn: sqlite3.Connection,
    intern_name: str,
    mentor_name: str | None = None,
    direction: str | None = None,
) -> int:
    row = conn.execute("SELECT id FROM pairs WHERE intern_name = ?", (intern_name,)).fetchone()
    if row:
        conn.execute(
            """
            UPDATE pairs
               SET mentor_name = COALESCE(?, mentor_name),
                   direction   = COALESCE(?, direction)
             WHERE id = ?
            """,
            (mentor_name, direction, row["id"]),
        )
        return int(row["id"])

    cur = conn.execute(
        "INSERT INTO pairs (intern_name, mentor_name, direction) VALUES (?, ?, ?)",
        (intern_name, mentor_name, direction),
    )
    return int(cur.lastrowid or 0)


def upsert_submission(conn: sqlite3.Connection, submission: Submission) -> tuple[int, str]:
    """Возвращает (id, статус), где статус — created | updated | duplicate.

    Ключ идемпотентности: (пара, роль, месяц). Тот же текст — дубль, другой — правка.
    """
    pair_id = get_or_create_pair(
        conn,
        submission.intern_name,
        submission.mentor_name,
        submission.direction,
    )
    digest = content_hash(submission)
    payload = json.dumps(submission.as_dict(), ensure_ascii=False)
    raw_text = " ".join(filter(None, [submission.q_better, submission.q_barrier]))

    row = conn.execute(
        "SELECT id, content_hash FROM submissions WHERE pair_id = ? AND role = ? AND month = ?",
        (pair_id, submission.role, submission.month),
    ).fetchone()

    if row:
        if row["content_hash"] == digest:
            return int(row["id"]), "duplicate"
        conn.execute(
            """
            UPDATE submissions
               SET payload_json = ?, raw_text = ?, content_hash = ?, source = ?,
                   received_at = ?, external_id = ?, status = 'updated'
             WHERE id = ?
            """,
            (
                payload,
                raw_text,
                digest,
                submission.source,
                submission.received_at,
                submission.external_id,
                row["id"],
            ),
        )
        return int(row["id"]), "updated"

    cur = conn.execute(
        """
        INSERT INTO submissions
            (pair_id, role, month, source, raw_text, payload_json,
             external_id, received_at, content_hash, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
        """,
        (
            pair_id,
            submission.role,
            submission.month,
            submission.source,
            raw_text,
            payload,
            submission.external_id,
            submission.received_at,
            digest,
        ),
    )
    return int(cur.lastrowid or 0), "created"


def log_unmatched(
    conn: sqlite3.Connection,
    source: str,
    raw: str,
    error: str,
) -> int:
    """Строка, которую не удалось привязать к паре. Не выбрасываем: на финале это улика."""
    cur = conn.execute(
        """
        INSERT INTO ingest_log (source, status, raw, error)
        VALUES (?, 'unmatched', ?, ?)
        """,
        (source, raw[:20000], error[:500]),
    )
    return int(cur.lastrowid or 0)


def count_rows(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("pairs", "submissions", "snapshots", "ingest_log")
    result = {}
    for table in tables:
        try:
            result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            result[table] = -1
    return result
