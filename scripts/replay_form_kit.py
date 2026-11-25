#!/usr/bin/env python3
"""Заливает мок-набор ответов в приёмник как настоящие ответы формы.

Каждая строка CSV превращается в JSON-RPC, который шлёт Яндекс Форма.
Так можно наполнить дашборд до того, как форма настроена, или после — чтобы
проверить, что экран показывает осмысленную картину.

    python scripts/replay_form_kit.py --dry-run
    python scripts/replay_form_kit.py --url http://localhost:8000/api/ingest
    python scripts/replay_form_kit.py --month M5          # только один срез
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

FACTOR_KEYS = ("motivation", "clarity", "mentor", "workload", "inclusion", "future")
SCORE_COLUMNS = ("Мотивация", "Ясность", "Наставник", "Нагрузка", "Включённость", "Будущее")
DEFAULT_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "form-kit" / "answers.csv"


def read_rows(path: Path, month: str | None, role: str | None, limit: int | None) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    if month:
        rows = [row for row in rows if row["Месяц"] == month]
    if role:
        rows = [row for row in rows if row["Роль"] == role]
    return rows[:limit] if limit else rows


def to_params(row: dict, key: str = "") -> dict:
    params = {
        "s_name": row["ФИО"],
        "s_mentor": row["Наставник ФИО"],
        "s_direction": row["Направление"],
        "s_month": row["Месяц"],
        "s_role": row["Роль"],
    }
    for column, factor in zip(SCORE_COLUMNS, FACTOR_KEYS):
        params[f"f_{factor}"] = row[column]
    params["q_better"] = row["Что улучшилось"]
    params["q_barrier"] = row["Что мешает"]
    if key:
        params["key"] = key
    return params


def post(url: str, params: dict, quiet: bool = False) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "submit", "id": 1, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Delivery-Id": uuid.uuid4().hex},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    result = payload.get("result", {})
    if not quiet:
        submission = result.get("submission", {}).get("status", "—")
        print(
            f"{params['s_month']:<3} {params['s_role']:<7} {params['s_name']:<16} "
            f"status={result.get('status'):<9} анкета={submission}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Заливка мок-набора ответов формы")
    parser.add_argument("--url", default="http://localhost:8000/api/ingest")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--month", help="только один месяц, например M5")
    parser.add_argument("--role", choices=["intern", "mentor"])
    parser.add_argument("--limit", type=int, help="первые N строк")
    parser.add_argument("--key", default="", help="значение INGEST_KEY, если задан")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет отправлено")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    rows = read_rows(Path(args.csv), args.month, args.role, args.limit)
    if not rows:
        print("нет строк под такие фильтры")
        return 1

    if args.dry_run:
        for row in rows[:3]:
            print(json.dumps(to_params(row, args.key), ensure_ascii=False, indent=2))
        print(f"\nвсего строк: {len(rows)} (показаны первые три)")
        return 0

    stats = {"created": 0, "updated": 0, "duplicate": 0, "unmatched": 0, "rejected": 0, "accepted": 0}
    for row in rows:
        try:
            result = post(args.url, to_params(row, args.key), quiet=args.quiet)
        except urllib.error.URLError as exc:
            print(f"нет связи: {exc}")
            return 1
        if result.get("status") == "rejected":
            stats["rejected"] += 1
            continue
        stats["accepted"] += 1
        submission = result.get("submission", {}).get("status")
        if submission in stats:
            stats[submission] += 1
        else:
            stats["unmatched"] += 1

    print()
    print(
        f"отправлено {len(rows)}: accepted={stats['accepted']}, "
        f"создано={stats['created']}, обновлено={stats['updated']}, "
        f"дубликатов={stats['duplicate']}, без пары={stats['unmatched']}, "
        f"отклонено={stats['rejected']}"
    )
    if stats["rejected"]:
        print("если rejected — на сервере задан INGEST_KEY, добавь --key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
