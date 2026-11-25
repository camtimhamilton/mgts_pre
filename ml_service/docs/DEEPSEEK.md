# P1.1: DeepSeek, только синтетические данные

Адаптер `app/deepseek.py` реализует существующий `LLMProvider.generate_structured` через HTTPX и OpenAI-compatible `POST /chat/completions`. OpenAI SDK не добавлен: HTTPX уже использовался проектом, теперь это runtime dependency. Mock остаётся default. Автоматического переключения deepseek→mock нет.

Документация сверена 2026-11-25: [первый запрос](https://api-docs.deepseek.com/), [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/), [JSON Output](https://api-docs.deepseek.com/guides/json_mode/), [thinking](https://api-docs.deepseek.com/guides/thinking_mode/), [HTTP errors](https://api-docs.deepseek.com/quick_start/error_codes/), [model details](https://api-docs.deepseek.com/quick_start/pricing/), [concurrency](https://api-docs.deepseek.com/quick_start/rate_limit/).

## Запуск

Из корня checkout, после установки uv/зависимостей:

```powershell
uv --cache-dir .uv-cache sync --frozen --extra dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.demo
```

Обычные тесты принудительно используют mock и поддельный HTTP transport, даже если shell настроен на DeepSeek. Demo/evaluate/export также явно выбирают FixtureProvider. `live` test отдельно требует opt-in и ключ. Ни ключ, ни настоящий ответ модели не нужны для модульных тестов.

Для DeepSeek API-сервиса задайте переменные **только в окружении процесса**. Файл `.env.example` документирует имена, не загружается автоматически. Не записывайте реальный ключ в `.env.example`/код/команды, которые попадут в историю терминала. Пример ввода ключа скрывает его при наборе:

```powershell
$secretKey = Read-Host 'DeepSeek API key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $secretKey).Password
$env:MGTS_PROVIDER = 'deepseek'
$env:MGTS_ALLOW_SYNTHETIC_LLM = '1'
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` должен вернуть `provider=deepseek`, `data_mode=demo`, `capabilities.real_provider=true`. Это проверка локальной конфигурации, **не проверка ключа/баланса/доступности DeepSeek**; health не вызывает сеть. Пример POST — тот же `tests/fixtures/survey_demo.json` и те же PowerShell/curl команды из README. Для произвольного текста теперь работает настоящая модель, но передача произвольного запроса API пока заблокирована политикой синтетики ниже.

Чтобы вернуться к автономному режиму, задайте `$env:MGTS_PROVIDER='mock'` и перезапустите процесс. Это явный выбор оператора, не fallback при ошибке. Ключ можно удалить из текущего окружения после остановки: `Remove-Item Env:DEEPSEEK_API_KEY`.

## Профили NLP и лимиты

| Env | Default | Значение |
|---|---|---|
| DEEPSEEK_SIGNAL_EXTRACTOR_MODEL | deepseek-flash | текущий Signal Extractor |
| DEEPSEEK_RULE_EXTRACTOR_MODEL | deepseek-v4-pro | профиль будущего Rule Extractor |
| DEEPSEEK_SEMANTIC_VALIDATOR_MODEL | deepseek-v4-pro | профиль будущего Semantic Validator |
| DEEPSEEK_{COMPONENT}_THINKING | 0 | 0/1, явно disabled/enabled в запросе |
| DEEPSEEK_{COMPONENT}_MAX_TOKENS | 4096 | бюджет вывода, локальный диапазон 1..32768 |
| DEEPSEEK_TIMEOUT_SECONDS | 30 | таймаут HTTPX на connect/read/write/pool |
| DEEPSEEK_TOTAL_TIMEOUT_SECONDS | 90 | полный deadline логического вызова, включая очередь/retries/backoff |
| DEEPSEEK_QUEUE_TIMEOUT_SECONDS | 2 | ожидание допуска к provider |
| DEEPSEEK_MAX_CONCURRENCY | 2 | одновременно допущенные логические вызовы provider |
| DEEPSEEK_MAX_RETRIES | 2 | максимум 2 повтора = 3 попытки; допустимо 0..3 |
| DEEPSEEK_RETRY_BASE_SECONDS | 0.5 | exponential backoff с jitter |
| DEEPSEEK_RETRY_MAX_SECONDS | 5 | предел backoff/Retry-After |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | разрешён также официальный /v1, произвольные хосты запрещены |

COMPONENT — SIGNAL_EXTRACTOR, RULE_EXTRACTOR или SEMANTIC_VALIDATOR. При thinking=1 задаётся `reasoning_effort=high`, temperature не отправляется. При thinking=0 — `temperature=0`; детерминизм повторных LLM-вызовов не гарантируется. Thinking явно выключен по default, потому что у провайдера он включён по умолчанию. Пример отдельного профиля:

```powershell
$env:DEEPSEEK_RULE_EXTRACTOR_MODEL = 'deepseek-v4-pro'
$env:DEEPSEEK_RULE_EXTRACTOR_THINKING = '1'
$env:DEEPSEEK_RULE_EXTRACTOR_MAX_TOKENS = '8192'
```

Адаптер принимает `component='rule_extractor'` или `'semantic_validator'` и любую схему будущего компонента. **Сами эти компоненты в P1.1 не реализованы**, strict по-прежнему не выполняет независимый V4 и возвращает text review. Их capability flags остаются false. Для нескольких одновременно работающих компонентов можно передать общий `asyncio.Semaphore` через аргумент `limiter`; иначе лимит относится к экземпляру provider. Лимиты локальны процессу, не аккаунту DeepSeek; несколько uvicorn workers увеличат совокупную конкурентность. Pipeline отдельно допускает до четырёх анализов.

## JSON, ошибки и повторные попытки

Отправляются `response_format={type:json_object}`, JSON Schema и пример формы в system prompt. Это **JSON Output, не серверный strict JSON Schema**. Адаптер требует непустой JSON-объект (не code fence, список, NaN или повтор ключей), а существующий `LLMExtraction` Pydantic затем обязательно проверяет доменные поля. Evidence Validator далее проверяет quote/role/rule provenance. Ошибки схемы и семантики не исправляются повтором/ослаблением проверки. Численный код и правила R10 не изменены.

| Сбой | Код / политика |
|---|---|
| 400 / 401 / 402 / 403 / 413 / 422 | invalid_request / authentication_failed / insufficient_balance / forbidden / request_too_large / invalid_parameters, без повторов |
| 408 / 429 / 500 / 502 / 503 / 504 | ограниченный повтор |
| HTTPX network / read timeout / protocol interruption | ограниченный повтор; запрос уже мог быть обработан/оплачен |
| пустой/пробельный content, включая stop | ограниченный повтор в общем бюджете |
| finish_reason=aborted/insufficient_system_resource | ограниченный повтор |
| finish_reason=length | provider_token_limit, без повтора и без автоматического увеличения бюджета |
| content_filter / tool_calls / неизвестный finish | явный отказ, без повтора |
| невалидный envelope/JSON/Pydantic | явный отказ, без повтора |
| общий deadline | provider_total_timeout; активная попытка отмечается cancelled |

Коды имеют префикс `provider_`. Retry-After поддерживает секунды и HTTP date. Если сервер просит ждать дольше локального максимума, адаптер возвращает ошибку, а не повторяет раньше указанного срока. Redirect не выполняется, proxy env отключён (`trust_env=False`). Ответ ограничен 2 MB, JSON content — 256 KB; запросы не имеют tools, web search или доступа к файлам. Reasoning content не сохраняется.

При пригодном rating и отказе NLP результат остаётся `partial`, `priority=insufficient_data`. Без полезного результата — прежний HTTP 503. Никакие rating/evidence/scenario правила не зависят от провайдера. Ошибки/обычные логи содержат только безопасные коды. HTTP error body, ключ, запросы, комментарии и сырые ответы не логируются. Диагностика не должна включать wire logging HTTP headers/bodies.

## Барьер синтетических данных

Нужен явный opt-in `MGTS_ALLOW_SYNTHETIC_LLM=1`. Он не означает разрешения обрабатывать данные МГТС. Перед внешним вызовом Pipeline проверяет **весь нормализованный SurveyPeriod** по SHA256 allowlist `tests/fixtures/synthetic_allowlist.json`: канонический demo и 20 конкретных synthetic cases. Изменение текста, ID, роли, месяца или контекста блокирует отправку с `synthetic_input_not_allowlisted` (HTTP 503). Изменение mapping/KB version отдельно проверяется как прежде. Hash не выводится в обычные логи.

Это allowlist доверенных локальных шаблонов, а не распознаватель персональных данных. Обновление каталога через `scripts.build_demo_fixtures` — осознанная локальная операция после проверки, что новые шаблоны синтетические. Не добавляйте реальные анкеты ради обхода барьера. Сам низкоуровневый DeepSeekProvider доверяет внутреннему вызывающему коду; arbitrary provider calls вне Pipeline также требуют ответственности за синтетику. Публичного endpoint для таких вызовов нет. Для будущих Rule Extractor/Validator потребуется их собственная политика egress; профили пока не дают публичного доступа к ним.

## Metadata и совместимость v1

Входы и пути REST, risk/factors/evidence/recommendations/scenario сохранены. `Metadata.provider` и health provider допускают дополнительное значение `deepseek`. Новые поля metadata имеют defaults и не требуют миграции старых snapshots:

- `provider_config` / `provider_config_hash`: неизменяемая эффективная конфигурация без ключа, включая модель/режим/лимиты и версию adapter.
- `provider_calls`: по каждому вызову requested model, model ID из ответа, fingerprint (если прислан), config/schema/effective prompt hashes, полная latency и список попыток.
- У попытки: latency, HTTP status, finish reason, model ID текущего ответа, sanitized error code, usage или null. Usage сохраняется и у неудачного ответа (например blank/length), если он получен. Отсутствующий usage не заменяется нулём; сетевой сбой не позволяет узнать фактическое списание. Итоговые расходы не прогнозируются.

`model_id` верхнего уровня — последний reported ID, при отсутствии ответа — настроенная requested model. В `provider_calls.model_identity_status` видно, вернул ли сервер alias, иной ID или ничего. `reported_id` означает только отличающееся имя из ответа, не доказательство immutable model snapshot. Alias `deepseek-flash` может менять underlying version; если API вернул только alias без fingerprint, точная серверная revision неизвестна. Документированная версия модели не подставляется в metadata как измеренная. Сохраняются requested model, ответ сервера и конфигурация; побитовая воспроизводимость live inference не заявляется.

Существующие prompt/KB/policy/mapping hashes/versions сохранены. Provider добавляет к system prompt JSON Schema/пример; поэтому отдельно сохранён **effective_prompt_hash** реально отправленных messages (включая user content), а прежний `prompt_hash` остаётся hash файла runtime prompt. Hash схемы и adapter version позволяют отследить обёртку. `extraction_hash` и `analysis_key` включают конфигурацию/identity/payload, но не случайные времена, latency или usage. Версия pipeline/package — 0.1.1, версия KB/policy остаётся 0.1.0.

`tests/fixtures/api_v1_p0_contract.json` фиксирует прежние доменные схемы/обязательные metadata поля; regression test сравнивает их с новым OpenAPI и читает старый snapshot. Никакие существующие поля не удалены и не переименованы. Клиентам, жёстко ограничивавшим provider значением mock, нужно разрешить deepseek; dashboard-поля скоринга не изменены. Обновлённый контракт — `docs/integration/openapi.json`.

## Отдельный реальный smoke

Это платный внешний запрос исключительно на фиксированном `survey_demo.json`. Он не запускается обычными тестами без opt-in, не читает произвольный input и не утверждает NLP accuracy.

```powershell
# DEEPSEEK_API_KEY уже задан скрытым вводом выше
$env:MGTS_ALLOW_SYNTHETIC_LLM = '1'
$env:MGTS_RUN_DEEPSEEK_SMOKE = '1'
.\.venv\Scripts\python.exe -m scripts.smoke_deepseek
# Или отдельный pytest smoke:
.\.venv\Scripts\python.exe -m pytest tests/test_deepseek_live.py -m live -v -rs
```

При отсутствии opt-in/ключа — `skipped` с причиной, без внешнего вызова. При выполнении скрипт печатает фактические latency/token usage/model identity, counts и observed score; тексты и ключ не печатает. Он проверяет transport/schema pipeline, а не фиксированный индекс 17: живая модель может вернуть иные или отклонённые классификации. Ошибка transport/schema даёт failed/nonzero; schema-valid, но слабое извлечение требует отдельной ручной оценки.

Live smoke не заменяет подтверждение правил HR, independent gold set, независимый semantic verifier или разрешение обработки данных МГТС.

Фактическая проверка P1.1 2026-11-25: 131 тест прошёл, один live test пропущен, один прежний upstream DeprecationWarning. С отдельным opt-in CLI и pytest smoke также вернули skipped: DEEPSEEK_API_KEY is absent. Реальных HTTP-вызовов, измеренных live latency/token usage и оценки NLP качества в этой сессии не было. Offline demo, проверка KB, compileall, dependency check и экспорт OpenAPI прошли.
