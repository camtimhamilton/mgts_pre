#!/usr/bin/env python3
"""Отправляет в приёмник ровно такой запрос, какой пришлёт Яндекс Форма.

Нужен, чтобы проверить сервер до того, как настраивать интеграцию в самой форме:
если этот скрипт даёт accepted и запись появляется в /api/ingest/log, значит
проблема в настройках формы, а не в приёмнике.

    python scripts/simulate_yandex.py
    python scripts/simulate_yandex.py --url http://[2a02:...]:8000/api/ingest --month M4
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid


def build_params(args: argparse.Namespace) -> dict:
    return {
        "s_name": args.name,
        "s_mentor": args.mentor,
        "s_direction": args.direction,
        "s_month": args.month,
        "s_role": args.role,
        "f_motivation": "4",
        "f_clarity": "3",
        "f_mentor": "5",
        "f_workload": "6",
        "f_inclusion": "5",
        "f_future": "4",
        "q_better": "Стало понятнее, какие задачи в приоритете.",
        "q_barrier": "Не понимаю, что от меня хотят: задачи меняются каждый день.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Имитация запроса Яндекс Формы")
    parser.add_argument("--url", default="http://localhost:8000/api/ingest")
    parser.add_argument("--name", default="Иванов Пётр")
    parser.add_argument("--mentor", default="Сидоров Алексей")
    parser.add_argument("--direction", default="Техподдержка")
    parser.add_argument("--month", default="M5")
    parser.add_argument("--role", default="intern", choices=["intern", "mentor"])
    parser.add_argument("--key", default="", help="значение INGEST_KEY, если он задан")
    args = parser.parse_args()

    params = build_params(args)
    if args.key:
        params["key"] = args.key

    body = json.dumps(
        {"jsonrpc": "2.0", "method": "submit", "id": 1, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Delivery-Id": uuid.uuid4().hex,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"нет связи: {exc}")
        print("проверь, что сервер слушает IPv6 и порт открыт: scripts/check_ipv6.sh")
        return 1

    result = payload.get("result", {})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()
    print(f"статус: {result.get('status')}, запись: {result.get('id')}")
    if result.get("submission"):
        print(f"анкета: {result['submission']}, анализ: {result.get('analysis')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
