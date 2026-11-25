"""Удаляет мок-данные из fixtures/form-kit из базы.

    python -m tools.clean_form_kit
    python -m tools.clean_form_kit --csv fixtures/form-kit/answers.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.db import connect, init_db

DEFAULT_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "form-kit" / "answers.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Удалить мок-набор ответов")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    args = parser.parse_args()

    with Path(args.csv).open(encoding="utf-8-sig", newline="") as handle:
        names = sorted({row["ФИО"] for row in csv.DictReader(handle, delimiter=";")})

    if not names:
        print("в файле нет данных")
        return

    init_db()
    conn = connect()
    try:
        placeholders = ",".join("?" for _ in names)
        pairs = conn.execute(
            f"DELETE FROM pairs WHERE intern_name IN ({placeholders})", names
        ).rowcount
        logs = 0
        for name in names:
            logs += conn.execute(
                "DELETE FROM ingest_log WHERE raw LIKE ?", (f"%{name}%",)
            ).rowcount
        conn.commit()
        print(f"удалено пар: {pairs}, записей журнала: {logs}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
