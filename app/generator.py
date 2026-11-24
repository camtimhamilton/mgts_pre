"""Генератор демо-данных: 12 пар × 6 месяцев × 2 роли.

Пять сюжетов задают траекторию пары, детерминированный шум делает профили
непохожими друг на друга, а открытые ответы подписаны так, чтобы совпадать
с сюжетом. Один и тот же seed всегда даёт один и тот же список анкет:

    from app.generator import generate
    submissions = generate(seed=42)

Генератор ничего не пишет в базу: это делает tools/seed_demo.py через app.repo.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .normalize import FACTORS, MONTHS, Submission

# Сюжеты идут по кругу. На 12 парах это даёт 3 stable, 3 quiet_growth
# и по 2 пары на gap, missing и rescue — все пять сюжетов и минимум по две пары.
STORIES: tuple[str, ...] = ("stable", "quiet_growth", "gap", "missing", "rescue")

STORY_TITLES: dict[str, str] = {
    "stable": "Стабильный",
    "quiet_growth": "Тихий рост",
    "gap": "Разрыв восприятия",
    "missing": "Пропущенная анкета",
    "rescue": "Спасение",
}

_INTERN_NAMES: tuple[str, ...] = (
    "Анна Ковалёва",
    "Дмитрий Соколов",
    "Мария Лебедева",
    "Артём Новиков",
    "Екатерина Волкова",
    "Иван Морозов",
    "Полина Зайцева",
    "Никита Павлов",
    "Ольга Семёнова",
    "Максим Фролов",
    "Алиса Егорова",
    "Роман Кузьмин",
)

_MENTOR_NAMES: tuple[str, ...] = (
    "Ирина Белова",
    "Сергей Орлов",
    "Наталья Крылова",
    "Павел Гусев",
    "Татьяна Никитина",
    "Алексей Смирнов",
    "Елена Виноградова",
    "Константин Барсуков",
    "Светлана Родина",
    "Денис Карпов",
    "Марина Титова",
    "Виктор Лавров",
)

_DIRECTIONS: tuple[str, ...] = (
    "Техподдержка",
    "Биллинг",
    "Аналитика",
    "Сети",
    "Мобильные",
    "Инфраструктура",
    "Контакт-центр",
    "ИТ-безопасность",
    "Проектный офис",
    "Разработка",
    "Абонентский сервис",
    "ЦОД",
)

_MONTH_TO_DATE: dict[str, tuple[int, int]] = {
    "M1": (2026, 2),
    "M2": (2026, 3),
    "M3": (2026, 4),
    "M4": (2026, 5),
    "M5": (2026, 6),
    "M6": (2026, 7),
}

_MSK = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class PairPlan:
    """Пара и её сюжет. Вынесено отдельно, чтобы тесты видели разметку сюжетов."""

    index: int
    story: str
    intern_name: str
    mentor_name: str
    direction: str
    missing_month: str | None = None


def plan_pairs(seed: int = 42, pairs: int = 12) -> list[PairPlan]:
    """Детерминированно распределяет сюжеты, имена и направления по парам."""
    if pairs < 0:
        raise ValueError("количество пар не может быть отрицательным")

    rnd = random.Random(seed)
    interns = list(_INTERN_NAMES)
    mentors = list(_MENTOR_NAMES)
    directions = list(_DIRECTIONS)
    rnd.shuffle(interns)
    rnd.shuffle(mentors)
    rnd.shuffle(directions)

    plans: list[PairPlan] = []
    for index in range(pairs):
        story = STORIES[index % len(STORIES)]
        intern_name = _unique_name(interns, index)
        mentor_name = _unique_name(mentors, index)
        direction = directions[index % len(directions)]
        if index >= len(directions):
            direction = f"{direction}-{index // len(directions) + 1}"
        plans.append(
            PairPlan(
                index=index,
                story=story,
                intern_name=intern_name,
                mentor_name=mentor_name,
                direction=direction,
                missing_month="M4" if story == "missing" else None,
            )
        )
    return plans


def _unique_name(names: list[str], index: int) -> str:
    name = names[index % len(names)]
    if index >= len(names):
        name = f"{name} {index // len(names) + 1}"
    return name


# ------------------------------------------------ траектории оценок


# Общий уровень пары по месяцам: 10 — хорошо, 0 — плохо.
_LEVELS: dict[str, tuple[float, ...]] = {
    "stable": (8.2, 8.0, 8.1, 7.9, 8.0, 8.2),
    "quiet_growth": (8.0, 7.2, 6.2, 5.1, 3.9, 3.0),
    "gap": (6.0, 6.0, 6.0, 6.0, 6.0, 6.0),
    "missing": (6.6, 6.4, 6.7, 6.3, 6.5, 6.6),
    "rescue": (6.6, 5.3, 3.5, 3.3, 5.2, 6.9),
}

# Сдвиги по факторам: у quiet_growth и rescue быстрее проседают нагрузка
# и уверенность в будущем, а отношения с наставником держатся дольше.
_FACTOR_SHIFT: dict[str, dict[str, float]] = {
    "stable": {
        "motivation": 0.3,
        "clarity": 0.1,
        "mentor": 0.4,
        "workload": -0.3,
        "inclusion": 0.2,
        "future": 0.2,
    },
    "quiet_growth": {
        "motivation": -0.3,
        "clarity": 0.2,
        "mentor": 0.6,
        "workload": -0.8,
        "inclusion": 0.1,
        "future": -0.5,
    },
    "gap": {
        "motivation": 0.0,
        "clarity": 0.0,
        "mentor": 0.0,
        "workload": 0.0,
        "inclusion": 0.0,
        "future": 0.0,
    },
    "missing": {
        "motivation": 0.2,
        "clarity": 0.0,
        "mentor": 0.3,
        "workload": -0.3,
        "inclusion": 0.1,
        "future": 0.0,
    },
    "rescue": {
        "motivation": -0.2,
        "clarity": 0.1,
        "mentor": 0.4,
        "workload": -0.7,
        "inclusion": 0.2,
        "future": -0.4,
    },
}

# Сюжет gap: стажёр видит проблемы, наставник — «всё нормально».
# По clarity и inclusion разрыв специально держится в коридоре 4–6 баллов.
_GAP_INTERN: dict[str, tuple[float, ...]] = {
    "motivation": (4.0, 4.0, 3.0, 3.0, 3.0, 2.0),
    "clarity": (4.0, 4.0, 3.0, 3.0, 2.0, 2.0),
    "mentor": (5.0, 5.0, 4.0, 4.0, 4.0, 3.0),
    "workload": (5.0, 4.0, 4.0, 3.0, 3.0, 3.0),
    "inclusion": (4.0, 4.0, 3.0, 3.0, 2.0, 2.0),
    "future": (4.0, 4.0, 3.0, 3.0, 2.0, 2.0),
}

_GAP_MENTOR: dict[str, tuple[float, ...]] = {
    "motivation": (7.0, 7.0, 7.0, 7.0, 7.0, 7.0),
    "clarity": (8.0, 9.0, 9.0, 8.0, 7.0, 8.0),
    "mentor": (7.0, 8.0, 7.0, 7.0, 7.0, 7.0),
    "workload": (7.0, 7.0, 7.0, 6.0, 6.0, 6.0),
    "inclusion": (8.0, 8.0, 9.0, 8.0, 6.0, 7.0),
    "future": (7.0, 7.0, 7.0, 7.0, 6.0, 6.0),
}

# Точные разрывы по месяцам для двух «чувствительных» факторов.
_GAP_DELTA: dict[str, tuple[int, ...]] = {
    "clarity": (4, 5, 6, 5, 5, 6),
    "inclusion": (4, 4, 6, 5, 4, 5),
}

def _stage(month_index: int) -> int:
    if month_index <= 1:
        return 0
    if month_index <= 3:
        return 1
    return 2


# ------------------------------------------------ открытые ответы

_STABLE_INTERN = (
    (
        "Наставник объясняет, зачем нужна каждая задача, — так гораздо понятнее.",
        "Иногда много однотипных заявок, но к концу дня успеваю.",
    ),
    (
        "Стало получаться делать заявки самому, без подсказок.",
        "Хочется больше времени на разбор сложных случаев, а не только на поток.",
    ),
    (
        "В команде спокойно: помогают, когда застреваю.",
        "Пока не хватает понятного плана на месяц, но это мелочи.",
    ),
    (
        "Разобрался в биллинге и теперь не боюсь сложных обращений.",
        "Иногда не хватает обратной связи по качеству, приходится догадываться.",
    ),
)

_STABLE_MENTOR = (
    (
        "Стажёр быстро освоился и берёт заявки без напоминаний.",
        "Пока не хватает времени на регулярную обратную связь, но это решаемо.",
    ),
    (
        "Есть прогресс: задаёт вопросы по делу и сам предлагает решения.",
        "Изредка путается в приоритетах, поправляем на лету.",
    ),
    (
        "Спокойно влился в команду, работает самостоятельно.",
        "Хотелось бы больше времени на разбор сложных случаев вместе.",
    ),
)

_QUIET_EARLY_INTERN = (
    (
        "Нравится, что задачи настоящие: сразу видно результат.",
        "Пока привыкаю к темпу, но в целом справляюсь.",
    ),
    (
        "Команда дружелюбная, объясняют, если что-то непонятно.",
        "Много новой информации, иногда теряюсь, но стараюсь.",
    ),
)

_QUIET_EARLY_MENTOR = (
    (
        "Вошёл в команду, берёт простые заявки.",
        "Пока требует подсказок, но учится.",
    ),
    (
        "Старается, задаёт вопросы, быстро схватывает.",
        "Пока не всё делает сам, но прогресс есть.",
    ),
)

_QUIET_MIDDLE_INTERN = (
    (
        "Стало больше самостоятельности, но и ответственности тоже.",
        "Задач становится больше, иногда не успеваю к дедлайну. "
        "Не всегда понимаю, что важнее.",
    ),
    (
        "Научился быстрее разбираться в заявках.",
        "Приходится задерживаться, чтобы всё закрыть. Устаю сильнее, чем ожидал.",
    ),
)

_QUIET_MIDDLE_MENTOR = (
    (
        "Прогресс есть, закрывает часть задач сам.",
        "Стало больше нагрузки на команду, обратной связи пока мало.",
    ),
    (
        "Берёт больше задач, но пока не всё успевает.",
        "Кажется, он устаёт, но времени разбирать это нет.",
    ),
)

_QUIET_LATE_INTERN = (
    (
        "Стал быстрее разбираться в заявках, но сил почему-то не прибавляется.",
        "Не вывожу нагрузку: заявки копятся, а конца не видно. "
        "Уже хожу по собеседованиям. Не понимаю, возьмут ли в штат.",
    ),
)

_QUIET_LATE_MENTOR = (
    (
        "Формально всё в порядке: задачи закрывает, в команде вежлив.",
        "Мне кажется, он справляется. Про перегруз спрашивать не принято.",
    ),
)

_GAP_INTERN_TEXT = (
    (
        "Хочу разобраться в биллинге, но пока больше вопросов, чем ответов.",
        "Не понимаю, что от меня хотят: задачи меняются каждый день. "
        "И не чувствую, что меня замечают в команде.",
    ),
)

_GAP_MENTOR_TEXT = (
    (
        "Всё отлично, стажёр включён в работу.",
        "Всё нормально, проблем не вижу. В команде всё отлажено.",
    ),
)

_MISSING_INTERN = (
    (
        "Постепенно вникаю в работу, наставник помогает.",
        "Иногда не успеваю за темпом, но стараюсь.",
    ),
    (
        "Стало понятнее, как устроен биллинг.",
        "Хочется больше ясности по приоритетам.",
    ),
    (
        "Команда приняла хорошо, работаю над своими задачами.",
        "Пока много рутины, но это часть работы.",
    ),
)

_MISSING_MENTOR = (
    (
        "Стажёр старается, постепенно берёт задачи сам.",
        "Не всегда хватает времени на обратную связь.",
    ),
    (
        "Прогресс есть, в команду влился.",
        "Пока путается в приоритетах, разбираем по ходу.",
    ),
    (
        "Справляется с простыми задачами, учится дальше.",
        "Нагрузка у команды высокая, стажёру достаётся меньше внимания.",
    ),
)

_RESCUE_EARLY_INTERN = (
    (
        "Нравится команда, задачи понятные.",
        "Пока много нового, но справляюсь.",
    ),
    (
        "Наставник подробно всё объясняет.",
        "Привыкаю к ритму, иногда устаю.",
    ),
)

_RESCUE_EARLY_MENTOR = (
    (
        "Вошёл в работу, старается.",
        "Пока требует много подсказок.",
    ),
    (
        "Есть интерес, задаёт вопросы.",
        "Пока не хватает самостоятельности.",
    ),
)

_RESCUE_CRISIS_INTERN = (
    (
        "Стал больше делать сам, но чувствую, что выдыхаюсь.",
        "Не вывожу нагрузку: беру задачи домой. "
        "Уже хожу по собеседованиям. Не понимаю, возьмут ли в штат.",
    ),
)

_RESCUE_CRISIS_MENTOR = (
    (
        "Стажёр берёт на себя больше задач.",
        "Вижу, что устаёт, но не хватает времени это разобрать.",
    ),
)

_RESCUE_RECOVERY_INTERN = (
    (
        "Помогли разобрать приоритеты, стало легче. "
        "Наставник стал больше времени уделять.",
        "Нагрузка ещё ощутимая, но уже есть план и поддержка.",
    ),
)

_RESCUE_RECOVERY_MENTOR = (
    (
        "Стали чаще встречаться, появился план развития.",
        "Нагрузка команды всё ещё высокая, но стажёр справляется лучше.",
    ),
)

_TEXT_BANK: dict[str, dict[str, dict[int, tuple[tuple[str, str], ...]]]] = {
    "stable": {
        "intern": {0: _STABLE_INTERN, 1: _STABLE_INTERN, 2: _STABLE_INTERN},
        "mentor": {0: _STABLE_MENTOR, 1: _STABLE_MENTOR, 2: _STABLE_MENTOR},
    },
    "quiet_growth": {
        "intern": {
            0: _QUIET_EARLY_INTERN,
            1: _QUIET_MIDDLE_INTERN,
            2: _QUIET_LATE_INTERN,
        },
        "mentor": {
            0: _QUIET_EARLY_MENTOR,
            1: _QUIET_MIDDLE_MENTOR,
            2: _QUIET_LATE_MENTOR,
        },
    },
    "gap": {
        "intern": {0: _GAP_INTERN_TEXT, 1: _GAP_INTERN_TEXT, 2: _GAP_INTERN_TEXT},
        "mentor": {0: _GAP_MENTOR_TEXT, 1: _GAP_MENTOR_TEXT, 2: _GAP_MENTOR_TEXT},
    },
    "missing": {
        "intern": {0: _MISSING_INTERN, 1: _MISSING_INTERN, 2: _MISSING_INTERN},
        "mentor": {0: _MISSING_MENTOR, 1: _MISSING_MENTOR, 2: _MISSING_MENTOR},
    },
    "rescue": {
        "intern": {
            0: _RESCUE_EARLY_INTERN,
            1: _RESCUE_CRISIS_INTERN,
            2: _RESCUE_RECOVERY_INTERN,
        },
        "mentor": {
            0: _RESCUE_EARLY_MENTOR,
            1: _RESCUE_CRISIS_MENTOR,
            2: _RESCUE_RECOVERY_MENTOR,
        },
    },
}


def _answers(plan: PairPlan, role: str, month_index: int, rnd: random.Random) -> tuple[str, str]:
    stage = _stage(month_index)
    return rnd.choice(_TEXT_BANK[plan.story][role][stage])


# ------------------------------------------------ сборка анкет


def _score(value: float) -> int:
    """Целая оценка 0–10."""
    return max(0, min(10, int(round(value))))


def _non_gap_scores(
    plan: PairPlan,
    month_index: int,
    profile: dict[str, float],
    pair_shift: float,
    rnd: random.Random,
) -> tuple[dict[str, int], dict[str, int]]:
    level = _LEVELS[plan.story][month_index]
    intern: dict[str, int] = {}
    mentor: dict[str, int] = {}
    for factor in FACTORS:
        base = level + _FACTOR_SHIFT[plan.story][factor] + profile[factor] + pair_shift
        intern[factor] = _score(base - 0.2 + rnd.randint(-1, 1))
        mentor[factor] = _score(base + 0.2 + rnd.randint(-1, 1))

    # Случайный шум не должен превращаться в «разрыв»: он есть только у сюжета gap.
    for factor in FACTORS:
        if mentor[factor] > intern[factor] + 2:
            mentor[factor] = min(10, intern[factor] + 2)
        elif intern[factor] > mentor[factor] + 2:
            mentor[factor] = max(0, intern[factor] - 2)
    return intern, mentor


def _gap_scores(
    month_index: int,
    profile: dict[str, float],
    rnd: random.Random,
) -> tuple[dict[str, int], dict[str, int]]:
    intern: dict[str, int] = {}
    mentor: dict[str, int] = {}
    for factor in FACTORS:
        bias = int(round(profile[factor] * 0.3))
        if factor in _GAP_DELTA:
            intern[factor] = _score(_GAP_INTERN[factor][month_index] + bias)
            gap = _GAP_DELTA[factor][month_index]
            mentor[factor] = max(0, min(10, intern[factor] + gap))
        else:
            intern[factor] = _score(
                _GAP_INTERN[factor][month_index] + bias + rnd.randint(-1, 1)
            )
            mentor[factor] = _score(
                _GAP_MENTOR[factor][month_index] + bias + rnd.randint(-1, 1)
            )
            # Остальные факторы не должны перебивать сигнал по clarity/inclusion.
            if mentor[factor] > intern[factor] + 3:
                mentor[factor] = min(10, intern[factor] + 3)
            elif intern[factor] > mentor[factor] + 3:
                mentor[factor] = max(0, intern[factor] - 3)
    return intern, mentor


def _month_scores(
    plan: PairPlan,
    month_index: int,
    profile: dict[str, float],
    pair_shift: float,
    rnd: random.Random,
) -> tuple[dict[str, int], dict[str, int]]:
    if plan.story == "gap":
        return _gap_scores(month_index, profile, rnd)
    return _non_gap_scores(plan, month_index, profile, pair_shift, rnd)


def _received_at(month: str, role: str, rnd: random.Random) -> str:
    year, month_number = _MONTH_TO_DATE[month]
    # Стажёр обычно отвечает в первой половине месяца, наставник — ближе к концу.
    if role == "intern":
        day = rnd.randint(5, 16)
    else:
        day = rnd.randint(15, 26)
    moment = datetime(
        year,
        month_number,
        day,
        rnd.randint(9, 18),
        rnd.randint(0, 59),
        tzinfo=_MSK,
    )
    return moment.isoformat(timespec="seconds")


def generate(seed: int = 42, pairs: int = 12) -> list[Submission]:
    """Возвращает до 12 × 6 × 2 анкет (у missing-пар на одну меньше).

    Один и тот же seed даёт идентичный список, включая порядок анкет,
    оценки, тексты и received_at.
    """
    plans = plan_pairs(seed=seed, pairs=pairs)
    rnd = random.Random(seed)
    submissions: list[Submission] = []

    for plan in plans:
        profile = {factor: rnd.uniform(-0.8, 0.8) for factor in FACTORS}
        pair_shift = rnd.uniform(-0.45, 0.45)

        for month_index, month in enumerate(MONTHS):
            intern_scores, mentor_scores = _month_scores(
                plan, month_index, profile, pair_shift, rnd
            )

            intern_better, intern_barrier = _answers(plan, "intern", month_index, rnd)
            submissions.append(
                Submission(
                    intern_name=plan.intern_name,
                    mentor_name=plan.mentor_name,
                    direction=plan.direction,
                    role="intern",
                    month=month,
                    scores=intern_scores,
                    origin="answer",
                    q_better=intern_better,
                    q_barrier=intern_barrier,
                    source="generator",
                    external_id=f"demo-{seed}-{plan.index:02d}-intern-{month}",
                    received_at=_received_at(month, "intern", rnd),
                )
            )

            # В сюжете missing наставник не сдал одну анкету; остальные месяцы на месте.
            if plan.missing_month == month:
                continue

            mentor_better, mentor_barrier = _answers(plan, "mentor", month_index, rnd)
            submissions.append(
                Submission(
                    intern_name=plan.intern_name,
                    mentor_name=plan.mentor_name,
                    direction=plan.direction,
                    role="mentor",
                    month=month,
                    scores=mentor_scores,
                    origin="answer",
                    q_better=mentor_better,
                    q_barrier=mentor_barrier,
                    source="generator",
                    external_id=f"demo-{seed}-{plan.index:02d}-mentor-{month}",
                    received_at=_received_at(month, "mentor", rnd),
                )
            )

    return submissions
