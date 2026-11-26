# Миграция к парной аналитике v2

Рабочая ветка `feature/pair-analytics-v2`, база интеграции `7ec0d99`.

- [x] V2-1 Сверить Git, постановку, две анкеты и книги. По 30 записей, в каждой книге 12 столбцов, роли имеют разные шкальные вопросы.
- [x] V2-2 Импорт 60 анкет и повторный импорт 60 дубликатов. Проверены привязка, редакции, реестр без анкет, повторная доставка, конфликты и миграция старых сценариев.
- [x] V2-3 Проверены независимые индексы, один набор пригодных наблюдений, сценарный union, обоснования баллов и исходные версии цитат. Контрольный сценарий 60/80/70 → 40/56/48.
- [x] V2-4 Запущена матрица на истории в режиме local_encoder. Headless Edge прошёл 29 проверок с реальными HTTP-процессами. Снимки и отчёты в data/review/.
- [x] V2-5 Сгенерированы схемы и синтетические примеры из работающего API. README содержит команды запуска. В MIGRATION_AUDIT.md собраны результаты, открытые вопросы и ограничения.
- [x] V2-6 Проведена локальная нейросетевая обработка истории и оценка на 24 синтетических фразах. Работает изолированный CLI предложений правил и рецензирования без автоматической активации.

Статус локальной проверки — backend 170 passed / 1 skipped, ML 195 passed / 1 skipped. Внешние зависимости для защиты — доступ к двум Яндекс Формам, публичный приёмник и ключ DeepSeek для разрешённой синтетики. Следующий шаг владельца — отправить по тестовому ответу из двух подключённых форм и сверить обновление исходных версий.

## Архив первой интеграции P0.1–P0.2

Ветка: integration/ml-dashboard, база origin/main 485f486. Исходный ML: origin/ml 46e74d0.
Общего предка нет; истории не объединялись. ML перенесён без перезаписи main в ml_service/.
Оригинальные main/ml остаются без изменений; push и PR не выполняются.

- [x] INT-1 Аудит: main FastAPI/SQLite/vanilla JS, ML FastAPI/Pydantic/YAML/SQLite. Baseline main 119 passed/1 skipped; ML 131 passed/1 skipped.
- [x] INT-2 Клиент и адаптер backend: ML единственный источник индекса, persistent cache/status, demo-вычисления только явным режимом. Typed nested response validation до сохранения; текущие/immutable результаты в отдельных ML таблицах, legacy данные не удаляются.
- [x] INT-3 UI: динамические факторы, priority/coverage/errors/null/history, сценарий по union без изменения факта. Стиль сохранён. Headless Edge проверил десять факторов, 22%, scenario5/fact17, M1=0/M2=— и защиту от stale response при смене месяца.
- [x] INT-4 Совместный smoke двух реальных HTTP процессов и регрессии; integration README/OpenAPI/mapping/фактические JSON и скриншот готовы.

Критерии: ingest→storage→HTTP ML→persist→GET dashboard; score=sum contributions; repeat GET/recompute не вызывают LLM заново при одинаковом входе; scenario не меняет факт и не удваивает вычет; missing/error не становятся нулём; история сохраняет пропуски, прогноз не имитируется.

Выявлено: корневые app/docs/scripts/tests/README/env конфликтуют; шесть dashboard аспектов не эквивалентны десяти ML факторам. Мотивация не маппится на «развитие» без основания. Mapping первой интеграции demo-only и версионируется на стороне ML. Неизвестный текст mock → partial; это не реальная NLP.

Проверка 2026-11-25: backend/dashboard **135 passed, 1 skipped**; ML **132 passed, 1 skipped**. Оба набора имеют одно прежнее upstream Starlette/AnyIO предупреждение. Совместный HTTP smoke passed: ingest, persistence, score17=sum contributions, coverage0.22, normalized gap 6/9, union A01+A11→5, GET/recompute cache, missing month и ML outage. Все действия A01+A03+A11 условно дают0, не24+5 двойного вычета. Browser smoke headless Edge passed без page errors; источник — реальные localhost сервисы, mock provider. compileall и dependency check прошли.

Независимое ревью: исправлены malformed nested ML response (раньше мог отравить cache), потерянный повторный импорт во время анализа и неверный подсчёт observation по всей программе. Все покрыты регрессиями. Browser выявил гонку ответа предыдущего месяца, исправлена request counters и проверена задержанным HTTP ответом.

Статус: **готово к локальному совместному ревью**. Точный исходный ML subtree зафиксирован коммитом82ac9a4 (treehash совпадает с origin/ml), дальнейшие изменения отдельно. Main/ml и remote refs не изменялись. Несвязанные истории не объединены. Push/PR не выполнялись.

Ограничения: mapping demo-only — только clarity/R03, workload/R04, inclusion/R07; составные mentor/motivation/future не угадываются. Поэтому полное покрытие и production-калибровка не заявляются. Temporal comparability/forecast, auth/tenant isolation, multiworker queue, force retry неизменённого partial и разрешение реальных данных — следующие задачи. Docker compose подготовлен, но daemon/compose plugin недоступны, build/run не проверены. Внешняя LLM не использовалась. Дальнейшие ML задачи: ml_service/TASKS.md.

Launcher integration/run.ps1 фактически запущен с -Seed -DashboardPort8865 -MLPort8866 -RunSeconds2: health двух сервисов прошёл, анкеты отправлены, оба процесса завершены с exit0; synthetic БД и логи оставлены в ignored data/integration/.

Следующий шаг: ревью с человеком1 и согласование вопросников/mapping, доступа и хранения. Запуск: integration/README.md; script integration/run.ps1 -Seed. Не начинать P1/P2 до принятия первой интеграции.
