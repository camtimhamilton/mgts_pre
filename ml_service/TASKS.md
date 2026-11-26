# Архив состояния ML v1

Актуальные задачи двухкомпонентной системы и результаты проверки находятся в [корневом TASKS.md](../TASKS.md).

Все данные, правила, веса и результаты P0 демонстрационные. Трекер проекта — этот файл.

- [x] P0-1 Контракты и конфигурация: строгий v1 вход/выход, проверяемая KB 0.1.0 и mapping demo-1. Проверены ссылки, роли, веса=100, шкалы, версии, hash; вложенные карты неизменяемы.
- [x] P0-2 Чистое ядро: provenance V1–V3, max внутри фактора, coverage, pending R10, рекомендации, union scenario. Проверены 17 / 0.22 / insufficient_data, сценарий 5; повторы и пересекающиеся действия не удваивают эффект.
- [x] P0-3 Автономный вертикальный срез: async fixture mock, orchestration, immutable SQLite snapshots, FastAPI/OpenAPI, безопасные события этапов; basic numeric gap=0.75. Отказы provider/storage и version conflict явные, partial не становится routine.
- [x] P0-4 Проверка и передача: 80 pytest passed, offline demo, 20 отрицательных/граничных fixtures, OpenAPI и 4 API sample в docs/integration, воспроизводимый README. Независимое read-only ревью проведено, замечания исправлены с регрессиями.

Статус: **P0 завершён в обязательном offline demo-объёме**. Реальные анкеты, бизнес-параметры, утверждённая KB, разрешения и интеграционный доступ неизвестны; эксплуатацию блокируют, demo не блокируют.

Фактическая проверка 2026-11-25 на Windows, CPython 3.13.7: `python -m pytest -q` — 80 passed, один upstream DeprecationWarning Starlette/AnyIO. `scripts.demo`, `scripts.validate_knowledge`, `scripts.evaluate` (20/20 scripted cases без ошибок), `scripts.export_contract`, `compileall` прошли. `uv sync --frozen --extra dev` и `uv pip check` — 23 совместимых пакета. Проверен editable install. `python` в PATH — нерабочий Windows alias, команды используют .venv/Scripts/python.exe. Python 3.12 не установлен; отклонение от предпочтительной версии описано в README/ARCHITECTURE.

Ограничения P0/P1.1: V4 не выполнен; fast text provisional, strict text → review. R10 pending, без баллов/интерфейса подтверждения. Fixture mock распознаёт только явно известные входы с hash KB/промпта; не является NLP. Метрики evaluate относятся только к scripted regression, не независимому gold set. DeepSeek HTTP adapter добавлен в P1.1 ниже. Поддерживается source checkout; standalone wheel с ресурсами, durable extraction artifacts/cache/replay, retention и auth пока не реализованы. SQLite хранит только локальные demo-результаты, включая цитаты; реальные данные запрещены до согласования. Групповая аналитика и интерфейс остаются у человека1.

Следующий шаг: интеграционный dry run с человеком1 по docs/integration и синтетическому запросу; после разрешения и получения реальной анкеты — mapping/KB и оценочные примеры.

P1: реальный mapping, проверенные/утверждённые знания и patch CLI; cache/replay, temporal/history, batch, Docker, согласование доступа с основным backend.
P2: независимая семантическая проверка, отдельный gold set 80–100, разрешённые live benchmarks.

## P1.1 — DeepSeek (реализован; внешний smoke ожидает ключ)

- [x] P1.1-1 Официальная документация проверена; env-конфигурация трёх моделей/лимитов, immutable config и allowlist 21 целого synthetic SurveyPeriod. Неизвестный вход блокируется до HTTP.
- [x] P1.1-2 Async HTTPX OpenAI-compatible adapter, JSON Output + Pydantic/существующая evidence validation; bounded retry/concurrency/deadline, latency/usage попыток, честная model identity. Ключ только env, нет raw error body в логах и fallback.
- [x] P1.1-3 API wiring и OpenAPI обновлены. Входы/доменные результаты не менялись; provider enum расширен, metadata дополнена optional полями. Regression сравнивает P0 схемы и читает старый snapshot.
- [x] P1.1-4 Добавлены 51 offline HTTP/config/integration regression test, отдельный opt-in live test и CLI smoke; инструкции docs/DEEPSEEK.md и .env.example. Все offline проверки выполнены. Live test выполнен как skipped — реальная доступность/качество провайдера не подтверждены.

Проверки 2026-11-25, CPython 3.13.7: **131 passed, 1 skipped**, один прежний upstream Starlette/AnyIO DeprecationWarning. `scripts.demo`: 17/0.22/0.75, scenario 5. `scripts.validate_knowledge`, `compileall app scripts`, export_contract и `uv pip check` (23 packages) прошли. uv.lock и requirements-dev.lock обновлены. Независимое read-only ревью проведено: исправлены потеря попытки при deadline, stale model ID между retries и mutable telemetry config; регрессии прошли.

Внешний smoke отдельно запущен с opt-in: CLI status=skipped, pytest=1 skipped, причина **DEEPSEEK_API_KEY is absent**. Внешних запросов и реальных latency/token usage измерений не было. Не заявлять проверенный баланс/ключ/точную model revision. Alias и optional fingerprint сохраняются как provider-reported, неизвестная revision не выдумывается.

Следующий шаг P1.1: задать ключ в env и выполнить инструкции synthetic smoke в docs/DEEPSEEK.md. Затем ручная проверка извлечения на разрешённой синтетике; Rule Extractor и Semantic Validator — отдельные следующие задачи, сейчас только их model profiles (capability=false). Данные МГТС остаются запрещены до согласования.
