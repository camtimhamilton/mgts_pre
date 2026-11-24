"""Наполнение базы демо-данными.

    python -m tools.seed_demo --seed 42 --reset

Пишет только через app.repo.upsert_submission, поэтому повторный запуск
с тем же seed не создаёт дублей: все анкеты возвращаются со статусом duplicate.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter

from app.db import connect, init_db
from app.generator import generate, plan_pairs
from app.repo import upsert_submission

# Сбрасываем ровно четыре таблицы из задачи. Зависимые factors, steps и analyses
# уходят каскадом, ingest_log остаётся: это журнал приёма, его не трогаем.
_RESET_TABLES: tuple[str, ...] = ("snapshots", "submissions", "outcomes", "pairs")


def reset(conn: sqlite3.Connection) -> None:
    """Очищает демо-данные, не задевая журнал приёма и прочие таблицы."""
    for table in _RESET_TABLES:
        conn.execute(f"DELETE FROM {table}")


def seed(conn: sqlite3.Connection, seed_value: int, pairs: int, do_reset: bool) -> Counter:
    if do_reset:
        reset(conn)

    submissions = generate(seed=seed_value, pairs=pairs)
    stats: Counter = Counter()
    for submission in submissions:
        _, status = upsert_submission(conn, submission)
        stats[status] += 1
    conn.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сгенерировать и записать демо-данные МГТС.")
    parser.add_argument("--seed", type=int, default=42, help="зерно генератора (по умолчанию 42)")
    parser.add_argument("--pairs", type=int, default=12, help="количество пар (по умолчанию 12)")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="перед записью очистить snapshots, submissions, outcomes и pairs",
    )
    args = parser.parse_args(argv)

    init_db()
    conn = connect()
    try:
        stats = seed(conn, seed_value=args.seed, pairs=args.pairs, do_reset=args.reset)
        plans = plan_pairs(seed=args.seed, pairs=args.pairs)
    finally:
        conn.close()

    print(
        "Сид завершён: "
        f"пар {len(plans)}, анкет {sum(stats.values())}; "
        f"created={stats['created']}, updated={stats['updated']}, "
        f"duplicate={stats['duplicate']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
