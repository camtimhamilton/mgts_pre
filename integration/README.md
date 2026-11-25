# P0.1–P0.2: совместный прототип

Ветка `integration/ml-dashboard` создана от `origin/main` **485f486**. ML импортирован
из `origin/ml` **46e74d0** в `ml_service/`. Исходные main/ml не менялись, push/PR не выполнялись.
Истории **не имеют общего предка**; merge unrelated histories не делался. Коммит импорта
`82ac9a4` содержит точное дерево ML (`d2fe4e86402dcc4136e75d0c543c93d72097c1c1`), с Source-commit
в сообщении. Оригинальная история ML остаётся доступна в ветке ml. Интеграционные изменения
идут следующим коммитом, отдельно от переноса.

## Что обнаружено при аудите

- Main — Python FastAPI 0.115.6, pydantic-settings, SQLite, HTTPX, openpyxl, vanilla
  JavaScript/CSS без frontend build. Ingest/upsert, CSV/XLSX import, demo generator,
  snapshots и SSE уже реализованы. Нет необходимости писать другой backend/frontend.
- Старый main считает шесть факторов, надбавку за разрыв и text signals, округляет
  вклады до целых, формирует свои шаги/редукции и линейные прогнозы. Старый LLM auto
  умеет real/fake fallback; это не контракт ML.
- ML — отдельный FastAPI 0.115.14/Pydantic 2.11.7, YAML KB, immutable SQLite snapshots,
  десять факторов R01–R10, max внутри фактора, weighted coverage, nullable score,
  recommendations и union scenario. DeepSeek adapter уже есть в ml, но не используется здесь.
- Корневые `app`, `tests`, `scripts`, `docs`, README/env конфликтовали. Два отдельных
  сервиса/venv сохраняют независимые импорты `app.*` и разные версии зависимостей.
- API ML не предоставляет temporal forecast, batch/history, human confirmation или
  independent semantic verifier. UI не должен показывать старый прогноз как результат ML.
- Реальные формулировки шести вопросов не полностью соответствуют десяти ML факторам.
  Ограниченный demo mapping, причины отказа и преобразование контрактов описаны в
  [dashboard_mapping.md](dashboard_mapping.md).

Default `ANALYSIS_MODE=ml`: единственный источник индекса/весов/порогов/мероприятий — ML.
Legacy `app/analysis.py`, `app/scoring.py`, `app/llm.py`, `app/demo_service.py` работают
только при **явном** `ANALYSIS_MODE=demo`; результаты в другой таблице, не смешиваются.
`LLM_MODE=fake` безопасен по умолчанию. Backend ML-клиент не вызывает внешнюю LLM.

## Локальный запуск (проверен на Windows, Python 3.13.7)

Из корня **этого integration worktree**:

```powershell
uv --cache-dir .uv-cache venv --python 3.13 .venv
uv --cache-dir .uv-cache pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
uv --cache-dir .uv-cache venv --python 3.13 ml_service/.venv
uv --cache-dir .uv-cache pip install --python ml_service/.venv/Scripts/python.exe -r ml_service/requirements-dev.lock
.\integration\run.ps1 -Seed
```

Скрипт запускает два скрытых процесса, ждёт health, отправляет две синтетические анкеты
и оставляет приложение работающим до Ctrl+C. Dashboard http://127.0.0.1:8000 ; ML Swagger
http://127.0.0.1:8001/docs . Данные и логи остаются в `data/integration/` (ignored).
При запрете исполнения PowerShell используйте два терминала ниже; менять execution policy не нужно.
`INGEST_KEY`, если ранее был задан в shell/.env, должен совпадать с ключом отправляемых форм;
для чистой синтетической демонстрации оставьте его пустым.

Терминал 1 (ML; пути ниже относительно ml_service):

```powershell
cd ml_service
$env:MGTS_PROVIDER='mock'
$env:MGTS_MAPPING_PATH='mappings/dashboard-demo.yaml'
$env:MGTS_FIXTURE_CATALOG='tests/fixtures/dashboard_mock_extractions.json'
$env:MGTS_DATABASE='data/integration-ml.sqlite3'
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
```

Терминал 2 (из корня):

```powershell
$env:ANALYSIS_MODE='ml'
$env:ML_API_URL='http://127.0.0.1:8001'
$env:ML_MAPPING_VERSION='dashboard-demo-1'
$env:LLM_MODE='fake'
$env:DB_PATH='data/integration-backend.sqlite3'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Синтетические анкеты: [synthetic_ingest.json](synthetic_ingest.json). Отправка после старта:

```powershell
$samples = Get-Content .\integration\synthetic_ingest.json -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($sample in $samples) {
  $body = [System.Text.Encoding]::UTF8.GetBytes(($sample | ConvertTo-Json))
  Invoke-RestMethod http://127.0.0.1:8000/api/ingest -Method Post -ContentType 'application/json; charset=utf-8' -Body $body
}
```

Ingest отвечает сразу, анализ идёт в фоне. Обновление UI — прежний SSE. При пустой БД
интеграция показывает пустые/недоступные данные, а не чужую fixture-карточку. Не открывайте
оба сервиса в одном Python-процессе: их исторические пакеты называются `app`.

Docker: `docker compose up --build` теперь описывает оба сервиса; backend ждёт health ML.
**Docker build/run в этой среде не проверены: daemon недоступен, compose plugin данного CLI также не готов**. Протестирован именно
локальный запуск обоих процессов. Не публикуйте эти endpoints с реальными данными:
auth/TLS/доступ к конфиденциальной обратной связи ещё требуют согласования команды.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location ml_service
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
.\.venv\Scripts\python.exe -m integration.smoke
# Обновить реальные synthetic JSON примеры/OpenAPI из выполненного smoke:
.\.venv\Scripts\python.exe -m integration.smoke --export
```

Отдельная браузерная проверка (не обязательна для запуска):

```powershell
uv --cache-dir .uv-cache pip install --python .venv/Scripts/python.exe -r integration/requirements-browser.txt
.\.venv\Scripts\python.exe -m integration.browser_smoke
```

Browser smoke использует установленный Microsoft Edge в headless режиме, а не скачивает
браузер. В Linux/macOS этот конкретный launcher не обещан; HTTP smoke поддерживает
venv/bin/python. Снимок реального синтетического экрана: [screenshots/ml-dashboard.png](screenshots/ml-dashboard.png).

Фактическая проверка 2026-11-25:

| Проверка | Результат |
|---|---|
| исходный main до изменений | 119 passed, 1 skipped |
| исходный ML | 131 passed, 1 skipped |
| backend/dashboard после интеграции | 135 passed, 1 skipped |
| ML после добавления mapping regression | 132 passed, 1 skipped |
| два реальных HTTP процесса | passed: ingest→ML→persist→dashboard, score17, coverage0.22, scenario5 |
| headless Edge | passed: 10 факторов, 22%, scenario5/fact17, M1=0, M2=—, stale-month-response guard, без page errors |
| compileall/dependency check | passed |
| PowerShell run.ps1 -Seed -RunSeconds 2 | оба сервиса запущены/остановлены, данные сохранены |
| Docker | не выполнен, daemon недоступен |

В pytest есть прежнее upstream предупреждение Starlette/AnyIO. Skip ML — отдельный
live DeepSeek test (opt-in выключен), main — прежний опциональный тест. Никаких
реальных внешних LLM запросов/качества NLP не измерялось.

Совместный smoke создаёт временные БД/порты и останавливает только собственные процессы.
Проверяет, что score=sum contributions, A01+A11 не удваивают вычет, сценарий не меняет
факт, повторные GET/recompute не создают ML snapshot, M2 не превращается в ноль, остановка
ML создаёт error/null только для нового расчёта, M3 остаётся читаемым. Экспортированные
[examples](examples) получены этим запуском, UUID/время не являются стабильным ожидаемым значением.

## Передача и следующие задачи

Контракт ML — [openapi.json](openapi.json), backend — [dashboard_openapi.json](dashboard_openapi.json).
Основной backend развивается в корневом app, ML — в ml_service; связь только HTTP.
Изменения API согласуются через versioned schemas и эти synthetic examples.

Перед совместным ревью обсудить с человеком1:

1. Фактический смысл составных mentor/future/motivation вопросов; разнести их или
   утвердить другой mapping/фактор. Сейчас они не получают выдуманный вклад.
2. Старые пороги/веса/надбавки и прогноз отключены в ML режиме; dashboard показывает
   баллы, priority и неполное покрытие. Это намеренное изменение методологии.
3. Как идентифицировать людей вместо текущего name-based pairing backend; какие
   права HR/наставника на цитаты и исходные ответы, хранение и аудит реальных данных.
4. Contract retry/rescore для неизменённого partial, инвалидация при новой KB/mapping,
   перенос/backup ML snapshots, auth между сервисами.
5. Temporal comparability и прогноз пока отсутствуют, human review/approved KB/
   independent semantic validation — следующие отдельные этапы. До них нельзя
   считать этот mock-прототип эксплуатационным кадровым инструментом.

Первая интеграция готовится к локальному совместному ревью, без push/PR и без изменений
main/ml. Итоговый актуальный статус — [TASKS.md](../TASKS.md).
