# Контракт API (v0.2)

Фаза 0: `health`, `ingest`, `ingest/log`, `events` работают по-настоящему; `program`,
`pairs`, `pairs/{id}`, `pairs/{id}/submissions` пока отдают фикстуры и помечены заголовком
`X-Data-Source: fixture`. Полная схема — `/docs` (OpenAPI).

---

## GET /api/health

```json
{
  "status": "ok",
  "app": "pulse",
  "version": "0.2",
  "phase": 0,
  "db": true,
  "view": "hr",
  "team": "ПУЛЬС",
  "demo_mode": true,
  "ts": "2026-11-25T00:10:00+00:00"
}
```

---

## POST /api/ingest

Единственный вход для формы. Тело — произвольный JSON. Заголовки:

| Заголовок | Обязателен | Смысл |
|---|---|---|
| `Content-Type: application/json` | да | иначе тело не разобрать, но ответ всё равно 200 |
| `X-Delivery-Id` | нет | склейка дубликатов; Яндекс Формы шлют его сами |
| `X-Ingest-Key` | если задан `INGEST_KEY` | защита от посторонних |

Ответ всегда `200`. Для JSON-RPC-запроса — в формате JSON-RPC:

```json
{"jsonrpc": "2.0", "result": {"ok": true, "id": 12, "duplicate": false, "status": "accepted"}, "id": 7}
```

Для обычного запроса — без обёртки:

```json
{"ok": true, "id": 12, "duplicate": false, "status": "accepted"}
```

Статусы в теле: `accepted` — принято, `duplicate` — уже было, `unparsed` — тело не JSON,
`rejected` — не сошёлся ключ. Все они означают HTTP 200.

---

## GET /api/ingest/log?limit=20

```json
{
  "total": 9,
  "items": [
    {
      "id": 9,
      "source": "webhook",
      "status": "accepted",
      "delivery_id": "abc",
      "error": null,
      "received_at": "2026-11-25 00:12:03",
      "size": 412,
      "raw": "{\"jsonrpc\":\"2.0\", ..."
    }
  ]
}
```

`raw` обрезается до 1200 символов для выдачи, в базе лежит до 20 000.

---

## GET /api/program?month=M5

```json
{
  "month": "M5",
  "months": ["M1", "M2", "M3", "M4", "M5"],
  "kpi": {
    "at_risk": 2,
    "total_pairs": 5,
    "avg_risk": 47,
    "gap_pairs": 2,
    "submitted": { "done": 9, "expected": 10 },
    "last_signal_sec": 18
  },
  "bands": { "low": 1, "watch": 2, "critical": 2 },
  "top_causes": [{ "key": "clarity", "label": "неясные задачи", "count": 3 }],
  "heatmap": [
    { "pair_id": 1, "name": "Иванов Пётр", "cells": [{ "month": "M1", "risk": 22 }] }
  ]
}
```

---

## GET /api/pairs?month=M5

```json
{
  "month": "M5",
  "items": [
    {
      "id": 1,
      "intern_name": "Иванов Пётр",
      "mentor_name": "Сидоров А.",
      "direction": "Техподдержка",
      "risk": 72,
      "delta": 10,
      "band": "critical",
      "urgency": 87,
      "gap": 6,
      "sparkline": [22, 31, 45, 62, 72],
      "missing": null,
      "photo_url": null
    }
  ]
}
```

`missing`: `null` | `"intern"` | `"mentor"` — чьей анкеты нет в этом месяце.
`urgency = risk + 1.5 × max(0, delta)` — по ней сортировка по умолчанию.

---

## GET /api/pairs/{id}?month=M5

Полная карточка. Ключевые поля:

| Поле | Смысл |
|---|---|
| `risk`, `delta`, `band` | индекс, изменение в п.п., полоса |
| `trend` | `{"key": "fast_growth", "label": "быстро растёт"}` |
| `summary` | 2–3 предложения от модели |
| `factors[]` | шесть факторов: `points`, `score_intern`, `score_mentor`, `gap`, `quote` |
| `gap_penalty`, `text_penalty` | надбавки; вместе с суммой `points` дают `risk` |
| `signals[]` | текстовые сигналы с цитатой (в топ-3 не входят) |
| `steps[]` | шаги: `reduction`, `owner`, `due` |
| `history[]` | точки по месяцам |
| `scenarios` | `base[]` и `with_actions[]`, каждая точка — `risk`, `low`, `high` |
| `observation` | `months`, `with_both_forms` — для честной подписи «мало данных» |
| `originals` | метаданные ответов обеих сторон |

Инвариант, который проверяется тестом в фазе 2:
`sum(factors[].points) + gap_penalty + text_penalty == risk`.

---

## GET /api/pairs/{id}/submissions

```json
{
  "pair_id": 1,
  "items": [
    {
      "id": 101,
      "role": "intern",
      "month": "M5",
      "source": "form",
      "received_at": "2026-11-24T10:12:00+03:00",
      "answers": { "f_motivation": 4, "f_clarity": 1 },
      "open": { "q_better": "…", "q_barrier": "…" }
    }
  ]
}
```

---

## GET /api/events

SSE. События: `hello`, затем `ping` каждые 15 секунд. Фаза 2 добавит `submission.received`
и `snapshot.ready` — фронт подписывается один раз и больше не меняется.

---

## Коды ошибок

В фазе 0 ошибок почти нет намеренно. Дальше: `404` — нет пары, `422` — не разобрался файл
импорта, `503` — нет базы. Приёмник (`/api/ingest`) из этого правила исключён: он всегда 200.
