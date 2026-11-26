# ML-сервис ПУЛЬС

Парный API v2 и совместный запуск описаны в [инструкции интеграции](../integration/README.md). Контракт v1 и его команды сохранены ниже.

## Предложения к экспертным правилам

Выполните из `ml_service`. Команды создают отдельные файлы, поэтому для следующей редакции задайте новое имя выхода.

```powershell
.\.venv\Scripts\python.exe -m scripts.rule_proposal draft --input tests/fixtures/rule_proposal_synthetic.json --author demo-author --output ../data/rule-proposals/draft.json
.\.venv\Scripts\python.exe -m scripts.rule_proposal review --input ../data/rule-proposals/draft.json --reviewer demo-reviewer --decision approved --version 2.0.2-review.1 --note "Проверена учебная формулировка" --output ../data/rule-proposals/reviewed.json
```

Оба результата сохраняют `active=false`. Человек проверяет смысл цитаты и выбирает, готовить ли отдельное изменение политики. Предложение содержит статус источника, версию, ссылку на исходное правило и перечень изменений. Для проверки адаптера на заранее зафиксированном синтетическом источнике доступна команда `extract-synthetic --author demo-author --output ../data/rule-proposals/remote-draft.json` с ключом DeepSeek и разрешением на синтетический вызов. Произвольный внешний ввод эта команда не принимает.

## Архив · P0

Автономный ML REST API для **экспертного индекса наблюдаемых проблем / приоритета поддержки**. Индекс не является вероятностью ухода, психологической характеристикой или оценкой пригодности. Веса, правила, анкеты и примеры — **демонстрационные, не согласованы с МГТС**.

**P1.1:** подключён реальный DeepSeek adapter; запуск, профили моделей, metadata и отдельный synthetic smoke описаны в [docs/DEEPSEEK.md](docs/DEEPSEEK.md). По умолчанию сохранён offline mock. Входные контракты и детерминированный расчёт не изменены.

Проверка P1.1: **131 passed, 1 skipped**; реальный smoke пропущен из-за отсутствия `DEEPSEEK_API_KEY`. Реальные метрики DeepSeek не измерялись. Контрольный offline расчёт 17/0.22 и сценарий 5 сохранены.

P0 работает локально без ключа и сети после установки зависимостей. Текстовый provider — **deterministic fixture mock** на 20 известных синтетических примерах, а не универсальная NLP-модель. Неизвестный текст даёт явную ошибку компонента; перехода к mock после ошибки реальной модели нет. Любые реальные данные пока не передавайте: разрешения, доступ и сроки хранения ещё не согласованы. Сам API не умеет определять происхождение переданного JSON.

## Установка в Windows PowerShell

Рабочая директория — корень этого checkout. Предпочтительно Python 3.12; в текущей среде фактически проверен **CPython 3.13.7**. Поддерживаемый диапазон в pyproject: >=3.11. Системный `python` здесь оказался нерабочим Windows alias, поэтому использован установленный `uv`.

```powershell
uv --cache-dir .uv-cache venv --python 3.13.7 .venv
uv --cache-dir .uv-cache sync --frozen --extra dev
.\.venv\Scripts\python.exe -m scripts.validate_knowledge
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.demo
```

Для 3.12 замените версию в первой команде; эта версия в текущей сессии не проверялась. `uv.lock` фиксирует полный граф зависимостей; `--frozen` не пересчитывает его. `requirements-dev.lock` — экспорт того же графа для pip. Вариант с установленным Python, без uv:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock -e '.[dev]'
.\.venv\Scripts\python.exe -m pip check
```

Установка требует доступа к пакетному индексу. После установки все проверки/demo используют только локальные файлы. Активация venv и изменение execution policy не нужны. Запуск поддерживается **из исходного checkout с editable install**: YAML, промпты и fixture-каталог лежат рядом с `app/`; отдельное распространение wheel без этих ресурсов пока не поддерживается.

## Что проверено

`pytest` проверяет чистое ядро, схему и API через TestClient, SQLite, fail-closed поведение и 20 известных текстовых fixtures: отрицания, прошлое/устранение/условность, неоднозначность, поддельные цитаты, дубли, пересечения, прямой R10 и мнение наставника, обычное завершение, prompt injection. Есть проверки отсутствия сети в mock, безопасных логов, provider identity, timeout, некорректного JSON и отказа хранилища. Тесты и demo не требуют фонового сервера.

Первоначальный прогон P0 2026-11-25: **80 passed**, demo и проверка KB успешны, `uv pip check` подтвердил совместимость 23 установленных пакетов. Один `DeprecationWarning` идёт из Starlette TestClient о переносе alias BlockingPortal в AnyIO; проверки не подавляются. Он не относится к выполнению доменного pipeline. `compileall` для app/scripts также прошёл. Независимое ревью выявило ошибки на границах R10, overlap, неизменяемости и типов чисел; они исправлены и покрыты регрессиями. Актуальный результат P1.1 — в начале README и TASKS.

Контрольный пример `tests/fixtures/survey_demo.json`:

| Поле | Ожидается |
|---|---|
| R01: high, вес 12 | вклад 12 (rating moderate не добавляется сверху) |
| R03: moderate, вес 10 | вклад 5 |
| score / coverage | 17 / 0.22 |
| score_band / priority | routine / insufficient_data |
| Оценки 2/5 и 5/5 | normalized_gap 0.75, без прибавки к индексу |
| A01 либо A01+A11 | условный сценарий 5, вычет уникального R01 = 12 |
| Повторное чтение snapshot | исходный score остаётся 17 |

Demo печатает JSON с этими числами и `provider=mock`, `synthetic=true`. UUID и времена меняются между запусками. В этом примере положительная оценка наставника и отрицательный текст стажёра сохраняются раздельно; R01 имеет состояние `conflicting`, без автоматического решения, кто прав.

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate
.\.venv\Scripts\python.exe -m scripts.export_contract
```

`evaluate` измеряет совпадение результата интеграции со scripted fixtures, disposition counts, latency p50/p95. Это **не независимый gold set и не измерение качества реальной LLM**. Precision/recall/F1 доступны только для меток, где есть наблюдения; остальные возвращаются null и перечисляются как uncovered. Семантическая правильность отдельно не оценивалась. Самостоятельный gold set и сравнение моделей — P2. `build_demo_fixtures` меняет каталог и привязку к hash KB/промпта: запускать только после явного пересмотра ожидаемых примеров, а не для обхода ошибки hash.

## HTTP и интеграция

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Swagger: http://127.0.0.1:8000/docs ; фактический контракт: http://127.0.0.1:8000/openapi.json . Сохранённая копия и проверенные синтетические запросы/ответы — [docs/integration](docs/integration). Их `analysis_id` иллюстративны: для сценария используйте ID, полученный от своего запущенного сервиса.

| Метод / путь | Поведение |
|---|---|
| GET /health | demo/provider, версии, готовность конфигурации и capability flags |
| POST /v1/analyze | SurveyPeriod → AnalysisResult, включая все 10 факторов |
| GET /v1/analysis/{analysis_id} | ранее сохранённый immutable snapshot |
| POST /v1/scenario | выбор доступных в snapshot действий → условный вычет по union факторов |

```powershell
$body = Get-Content .\tests\fixtures\survey_demo.json -Raw -Encoding UTF8
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$analysis = Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/analyze -Method Post -ContentType 'application/json; charset=utf-8' -Body $bytes
$analysis.risk
$scenario = @{analysis_id=$analysis.analysis_id; action_ids=@('A01','A11')} | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/scenario -Method Post -ContentType 'application/json' -Body $scenario
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/analysis/$($analysis.analysis_id)"
```

Альтернатива curl в PowerShell (именно `curl.exe`, не alias):

```powershell
curl.exe -sS http://127.0.0.1:8000/health
curl.exe -sS -H 'Content-Type: application/json' --data-binary '@tests/fixtures/survey_demo.json' http://127.0.0.1:8000/v1/analyze
```

Нормализовать исходные выгрузки, разрешать исправления/дубли анкет и связывать людей должен основной backend. Вход содержит opaque IDs, месяц 1..6, не более одной актуальной анкеты каждой роли, вопросы и типизированные ответы. Пропуск — отсутствие ответа или `value:null`; 0 не заменяет пропуск. `context` разрешает только boolean `training_plan_confirmed`; этот факт зарезервирован, исполняемых исключений на его основе в P0 нет. Не передавайте ФИО и контакты.

Численные вопросы сопоставляются только по `role+question_id` в `mappings/demo.yaml`, с проверкой шкалы и явной таблицей значений. Неизвестное сопоставление, несовпадение шкалы или дробная оценка вне таблицы не превращаются в нулевой риск. Для нескольких вопросов одного comparison_key требуется отдельная политика; сейчас они маркируются `rating_pair_not_comparable`. Требуемые роли считаются присутствующими по принятым факторным свидетельствам, а не по пустой анкете. Это отдельная проверка полноты, не response rate.

Ошибки имеют `{code,message,trace_id,details}`; `details` содержит пути/типы ошибок, без копирования отвергнутого ввода. 422 — схема/недоступный режим, 409 — несовместимая версия или сценарий без score, 404 — snapshot/действие не найдены, 413 — тело >300 KB, 503 — нет полезного результата после сбоя provider, заняты слоты или недоступна БД. Если rating пригоден, а текстовый компонент сломан, возвращается `partial` с наблюдаемым score и `priority=insufficient_data`. `status=completed` означает завершение обработки, не полное покрытие анкеты. При отсутствии meaningful observations score=null. `X-Trace-ID` связывает обычный HTTP-ответ с событиями.

## Режимы и объяснимость

По умолчанию `mode=live&profile=fast`: каждый раз вызывается **mock**, без replay/cache. `mode=live` описывает выполнение выбранного provider. `mode=replay` пока отклоняется (422), capability=false. `MGTS_PROVIDER` принимает `mock` или `deepseek`; для DeepSeek требуются ключ, synthetic opt-in и allowlisted synthetic input. Ошибка конфигурации останавливает старт; fallback отсутствует. `.env.example` документирует env; автоматического чтения `.env` нет.

Fast проверяет schema, ссылки, роли и точную принадлежность цитаты ответу. Offsets — Python Unicode code points [start,end); JavaScript должен преобразовать их к UTF-16. Несколько одинаковых вхождений цитаты требуют review. Пересекающиеся цитаты разных факторов требуют review; повтор фактора не увеличивает вклад. Фактор получает максимум severity, индекс — сумму вкладов, без нормализации по покрытию. Coverage — сумма весов покрытых факторов / 100, не доля заполненных вопросов. Порог demo=0.6. Противоречия не компенсируют отрицательный вклад, но блокируют обычный приоритет и видны в warnings/state.

**Наличие цитаты не доказывает семантическую классификацию.** Текст имеет `semantic_verification=not_run`, принятое в fast свидетельство предварительное. `profile=strict` оставляет все текстовые кандидаты в review: независимый verifier ещё не реализован. Численные mappings сохраняются. Прямой актуальный R10 стажёра всегда pending confidential review и не начисляет баллы до подтверждения; в P0 нет интерфейса утверждения. Исторический/условный R10, мнение наставника и обычное завершение не становятся критическим установленным фактом. KB содержит эти семантические границы, но произвольный ошибочный вывод будущей LLM всё ещё потребует V4/human review.

Сценарий принимает только analysis_id и action_ids, берёт веса/targets из сохранённого результата и не переписывает его. `assumption=full_resolution` — условие полного подтверждённого устранения, не обещанный эффект мероприятия. Выполнение действия само по себе индекс не меняет.

## Устройство и хранение

- `app/schemas.py`: публичные Pydantic v2 контракты и неизменяемые вложенные карты.
- `app/core.py`: чистые validation/rating/aggregation/scoring/gap/recommendation/scenario функции.
- `app/knowledge.py`, `knowledge_base/v0.1.0/`, `mappings/`: safe YAML, проверка ссылок, версий, весов и шкал; content hashes.
- `app/llm.py`, `prompts/`: async provider port, строгий локальный parse и fixture adapter; `app/deepseek.py`, `app/config.py`, `app/synthetic.py` — HTTP adapter, профили и egress guard.
- `app/orchestrator.py`: bounded execution, этапы, sanitized errors и metadata.
- `app/storage.py`: SQLite snapshot port; `app/main.py`: тонкий FastAPI с DI factory.

В metadata есть hashes input/KB/mapping/prompt/extraction, версии pipeline/validation и отдельный analysis_key. Повторный запуск сохраняет новый run snapshot с тем же ключом расчёта при одинаковом входе и результате извлечения; случайные ID/время не являются детерминированным расчётом. Одна запись по analysis_id идемпотентна, изменение запрещено. Extraction artifacts пока существуют в памяти для hash; отдельного durable extraction cache/replay нет.

SQLite по умолчанию `data/demo.sqlite3`; путь задаётся `MGTS_DATABASE`. Snapshot содержит цитаты и псевдонимы: это локальное **демо-хранилище**, не кадровая мастер-система. Сырые анкеты не сохраняются, но цитаты также потенциально чувствительны. Автоматическая очистка/retention, tenant isolation, доступ к реальным данным пока не реализованы. Удаление demo-БД после остановки сервиса удаляет snapshots; текущие запросы не зависят от прошлых.

Логи используют стандартный logging: только stage/status/trace_id/error_code/count, без текста, идентификаторов людей, секретов и input hashes. События в результате содержат времена, версии и ссылки на тип артефакта. Не более 4 активных анализов, ожидание admission до 0.1 секунды, до двух последовательных provider calls на анализ. Mock ограничен 10 секундами; DeepSeek имеет настраиваемые concurrency/deadline/retries (см. docs/DEEPSEEK.md). API ограничивает тело и число/размер полей; полноценный rate limiter не реализован. Сервис запускать на localhost/в закрытой сети; аутентификация, авторизация HR, TLS и внешний rate limit должны быть согласованы с основным backend до эксплуатации.

## Границы P0 и следующий шаг

Не реализованы применение знаний/patch, real export adapter, независимая семантика, история/динамика, semantic gaps, replay/cache, batch, Docker. Они явно отмечены capability=false или в TASKS, а не имитируются пустыми успешными результатами. DeepSeek доступен только для allowlisted синтетики. Библиографические источники — `reference_only`; никакие страницы/цитаты исследований не заявлены проверенными. Формулировки демо-правил и веса принадлежат автору проекта.

Скриншот dashboard принят как контекст, а не новая спецификация скоринга. Поля факторов, расхождений, evidence и мероприятий уже доступны. Групповые карточки, графики по месяцам, response rate, статусы выполнения действий и интерфейс делает основной backend; временная аналитика ML остаётся P1. Значения и названия факторов на скриншоте не заменяют согласованный словарь R01–R10.

Следующая итерация: совместно с человеком1 проверить синтетический контракт, затем адаптировать mapping и разрешённые данные/правила после получения реальной анкеты и решения МГТС о передаче и хранении.
