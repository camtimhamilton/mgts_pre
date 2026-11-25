"""Пересчёт снапшотов из командной строки.

    python -m tools.recompute            # всё
    python -m tools.recompute --pair 3   # одну пару, все месяцы
    python -m tools.recompute --pair 3 --month M5
"""

from __future__ import annotations

import argparse

from app import service
from app.db import connect, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Пересчитать снапшоты")
    parser.add_argument("--pair", type=int, help="id пары")
    parser.add_argument("--month", help="месяц, например M5")
    args = parser.parse_args()

    init_db()
    conn = connect()
    try:
        if args.pair and args.month:
            data = service.recompute_pair_month(conn, args.pair, args.month)
            if data is None:
                print(f"нет анкет: пара {args.pair}, {args.month}")
                return
            print(
                f"пара {args.pair}, {args.month}: риск {data['risk']}, "
                f"дельта {data['delta']:+d}, полоса {data['band']}"
            )
        elif args.pair:
            data = service.recompute_pair(conn, args.pair)
            print(f"пара {args.pair}: снапшотов {len(data)}")
        else:
            result = service.recompute_all(conn)
            print(f"пар {result['pairs']}, снапшотов {result['snapshots']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
