# Контракт фазы 2 — расчёт и LLM

Заморожен. Агенты пишут по нему, интегратор собирает. Менять сигнатуры нельзя:
на них уже завязаны `app/analysis.py`, `app/service.py` и API.

---

## 1. `app/scoring.py` — чистая арифметика

Ни БД, ни сети, ни LLM. Только числа. Все константы — модульные, чтобы их можно было
подкрутить на финале.

```python
FACTORS: tuple[str, ...]                 # из app.normalize
WEIGHTS: dict[str, float]                # 25/20/20/15/10/10, сумма 100
MAX_REDUCTION: dict[str, float]          # motivation 10, clarity 8, mentor 8,
                                         # future 7, workload 6, inclusion 5
TEXT_SIGNALS: dict[str, float]           # left_intent 8, burnout 5, offer_doubt 5, conflict 4
TEXT_CAP: float = 12.0
GAP_MIN: float = 2.0
GAP_STEP: float = 4.0
GAP_CAP: float = 20.0
STEP_CAP: float = 25.0
BAND_LOW: float = 34.0
BAND_HIGH: float = 60.0


@dataclass(frozen=True)
class Factor:
    key: str
    points: float              # вклад в риск, 0..вес
    score_intern: float | None
    score_mentor: float | None
    gap: float | None
    origin: str                # answer | mapped | estimated


def evaluate(
    intern: dict[str, float],
    mentor: dict[str, float] | None = None,
    origin: str = "answer",
) -> list[Factor]:
    """Среднее по доступным сторонам, вклады по весам.
    Если фактора нет ни у одной стороны — он не попадает в список.
    Если сторон две — gap = |intern - mentor|, иначе None.
    """

def gap_penalty(factors: list[Factor]) -> float:
    """max(|gap|) по факторам: clamp((max_gap - GAP_MIN) * GAP_STEP, 0, GAP_CAP)."""

def text_penalty(signals: list[dict]) -> float:
    """min(TEXT_CAP, сумма весов уникальных сигналов). Неизвестный ключ — 0."""

def risk(factors: list[Factor], gap: float, text: float) -> int:
    """round(clamp(sum(points) + gap + text, 0, 100))."""

def band(value: int) -> str:
    """low | watch | critical по BAND_LOW и BAND_HIGH."""

def urgency(risk_value: int, delta: int) -> float:
    """risk + 1.5 * max(0, delta)."""

def scenario(history: list[tuple[str, int]], reductions: float, horizon: int = 1) -> dict:
    """{"base": [{month, risk, low, high}], "with_actions": [...], "event": str | None}

    history — [(месяц, риск)] по возрастанию, последняя точка текущая.
    Экстраполяция: наклон по последним ≤3 точкам, затухание 0.7 на шаг,
    коридор ±max(6, 12 - 2*n), обрезка 0..100.
    reductions — сумма эффектов шагов, ограниченная STEP_CAP.
    event — первый месяц, где базовый сценарий уходит выше BAND_HIGH,
    текст вида «красная зона к M5»; None, если не уходит.
    При n < 2 — плоский прогноз. Если следующий месяц за пределами M1..M6 —
    пустые списки и event=None.
    """
```

Числа обязаны сходиться: `sum(f.points) + gap + text == risk`.

Тесты: границы 0/100, одна сторона, пустой месяц, отсутствие фактора, затухание,
коридор, событие, `urgency`, монотонность.

---

## 2. `app/llm.py` — только текст, никогда числа

Клиент двух провайдеров за одним интерфейсом, JSON-схема, ретрай, таймаут, кэш.
Режим определяется `LLM_MODE` (`auto` | `fake` | `real`): в `auto` при пустом ключе
работает `fake`, чтобы дашборд и тесты жили без сети.

```python
@dataclass
class Extraction:
    summary: str
    signals: list[dict]      # {"key": str, "quote": str, "confidence": float}
    quotes: list[dict]       # {"factor": str, "quote": str}
    flags: list[str]


@dataclass
class Explanation:
    summary: str
    reasons: list[dict]      # {"key": str, "text": str, "quote": str}
    steps: list[dict]        # {"key": str, "text": str, "owner": str, "due": str}
    gap_comment: str


def extract(text: str, *, role: str, month: str,
            answers: dict[str, float] | None = None) -> Extraction

def explain(context: dict) -> Explanation

def provider_name() -> str
```

`context` для `explain`:

```json
{
  "intern_name": "Иванов Пётр",
  "month": "M5",
  "factors": [
    {"key": "clarity", "label": "Ясность задач", "points": 12,
     "score_intern": 1, "score_mentor": 7, "gap": 6, "quote": "не понимаю, что от меня хотят"}
  ],
  "signals": [{"key": "left_intent", "label": "намерение уйти", "quote": "…"}],
  "intern_text": "…", "mentor_text": "…"
}
```

Правила:

- модель не возвращает `points`, `reduction`, `risk` — только формулировки;
- `quote` обязана быть дословной подстрокой исходного текста (проверка на бэкенде,
  но `fake` и `real` обязаны её соблюдать);
- `reasons` — не больше трёх, ключи только из `FACTORS`;
- `steps` — 2–3 штуки, ключ фактора из `FACTORS`, `owner` из `{HR, наставник}`,
  `due` — человеческий срок («2 недели», «1 месяц»);
- при любой ошибке сети/схемы — исключение `LLMError`, вызывающий решает, что делать;
- кэш по SHA-256 от (режим, провайдер, модель, промпт-версия, входные данные),
  таблица `llm_cache` в SQLite, повторный вызов — 0 мс и без сети;
- `fake` детерминирован: одинаковый вход → одинаковый выход, цитаты вырезаются
  из исходного текста, числа не выдумываются.

Тесты: схема ответа, детерминизм `fake`, кэш (счётчик вызовов провайдера), обрезка
невалидных цитат, `LLMError` при недоступном провайдере.
