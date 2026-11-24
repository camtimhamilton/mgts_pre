# Пульс программы

Дашборд мониторинга шестимесячной стажировки МГТС: одна страница, индекс риска по каждой
паре «стажёр–наставник», объяснение причин, шаги и сценарии.

Состояние: **фаза 0** — скелет, приёмник, контракт API на фикстурах. Расчёта ещё нет.

## Быстрый старт

```bash
cp .env.example .env
make up          # docker compose up --build -d
open http://localhost:8000
```

Без Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make dev         # uvicorn с автоперезагрузкой
```

## Что уже работает

| Что | Где |
|---|---|
| Живость сервиса и базы | `GET /api/health` |
| Приём ответов формы | `POST /api/ingest` |
| Сырой лог приёма (главный инструмент отладки на финале) | `GET /api/ingest/log` и главная страница |
| Резервная форма | `/form` |
| Контрактные заглушки | `/api/program`, `/api/pairs`, `/api/pairs/{id}` |
| SSE-канал | `GET /api/events` (пока heartbeat) |
| OpenAPI | `/docs` |

## Приёмник

Сделан под реальные ограничения Яндекс Форм, а не под идеальный вебхук:

- отвечает сразу, разбор и пересчёт — после ответа;
- **всегда 200**, даже на неразобранное тело: 404 и 5XX заставляют форму повторять запрос;
- дедупликация по `x-delivery-id`;
- сырой payload сохраняется целиком.

Подробности: [docs/form-channel.md](docs/form-channel.md).

Проверка вручную:

```bash
curl -s -X POST localhost:8000/api/ingest \
  -H 'content-type: application/json' \
  -H 'x-delivery-id: demo-1' \
  -d '{"jsonrpc":"2.0","method":"submit","id":1,"params":{"f_clarity":3}}'
```

## База

SQLite лежит в `./data/app.db`, каталог создаёт контейнер. Так как контейнер работает
от root, файл будет root-овым: сбросить состояние — `docker compose down -v && sudo rm -rf data`.
Для демо это плюс: файл можно скопировать и приложить к презентации.

## Структура

```
app/            FastAPI, схема БД, статика без сборки
fixtures/       контрактные примеры для фронта
tests/          смоук
docs/           спека, план, анкеты, канал приёма
```

## Документы

- [docs/spec.md](docs/spec.md) — что строим и почему
- [docs/plan.md](docs/plan.md) — порядок работ и критерии приёмки
- [docs/forms.md](docs/forms.md) — анкеты и идентификаторы полей
- [docs/form-channel.md](docs/form-channel.md) — как ответы попадают в дашборд
- [docs/api.md](docs/api.md) — контракт API

## Дальше

Фаза 1: импорт XLSX, генератор демо-данных 12×6, идемпотентность. Затем расчёт и карточка.
