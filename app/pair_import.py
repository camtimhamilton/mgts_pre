"""Strict import of the two supplied MGTS workbook layouts."""
from pathlib import Path

from openpyxl import load_workbook

from .db import write_lock
from .pair_models import SurveyInput
from .pair_store import accept, connect, digest, init_store, register_pair

MENTOR_HEADERS = ["ID наставника", "ID стажера", "Подразделение / направление стажировки", "Контрольный период",
                  "Понимание задач и ожиданий", "Качество выполнения задач", "Самостоятельность",
                  "Соблюдение сроков и организация работы", "Вовлеченность и инициативность",
                  "Взаимодействие с наставником и командой", "Комментарий о работе за период",
                  "Сложности / ситуации для внимания"]
INTERN_HEADERS = ["ID стажера", "Подразделение / направление стажировки", "Контрольный период",
                  "Понятность задач", "Поддержка и обратная связь наставника", "Комфортность нагрузки",
                  "Интерес к задачам", "Профессиональное развитие", "Взаимодействие с командой",
                  "Общая удовлетворенность стажировкой", "Сложности за период", "Что мешает работать и развиваться"]
RATINGS = {
    "mentor": ["clarity", "quality", "autonomy", "deadlines", "initiative", "team"],
    "intern": ["clarity", "support", "workload", "interest", "growth", "team", "satisfaction"],
}
TEXTS = {"mentor": ["comment", "attention"], "intern": ["difficulties", "barriers"]}


def read_workbook(path: str | Path, role: str) -> list[tuple[SurveyInput, dict]]:
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("expected_one_worksheet")
        rows = iter(book.active.values)
        headers = list(next(rows))
        expected = MENTOR_HEADERS if role == "mentor" else INTERN_HEADERS
        if headers != expected:
            raise ValueError(f"unexpected_{role}_headers")
        result = []
        seen = set()
        for number, row in enumerate(rows, start=2):
            if all(value is None for value in row):
                continue
            if any(isinstance(value, str) and value.startswith("=") for value in row):
                raise ValueError(f"formula_in_source_row_{number}")
            offset = 4 if role == "mentor" else 3
            period = row[offset - 1]
            if not isinstance(period, str) or period not in {f"Месяц {m}" for m in range(1, 7)}:
                raise ValueError(f"invalid_period_row_{number}")
            ratings = {key: row[offset + i] for i, key in enumerate(RATINGS[role]) if row[offset + i] is not None}
            text_offset = offset + len(RATINGS[role])
            texts = {key: row[text_offset + i] for i, key in enumerate(TEXTS[role]) if row[text_offset + i] is not None}
            survey = SurveyInput(
                mentor_id=row[0] if role == "mentor" else None,
                intern_id=row[1] if role == "mentor" else row[0],
                department=row[offset - 2], role=role, month=int(period[-1]), ratings=ratings, texts=texts,
            )
            logical_key = (survey.intern_id, survey.month)
            if logical_key in seen:
                raise ValueError(f"duplicate_or_conflicting_{role}_row_{number}")
            seen.add(logical_key)
            result.append((survey, {"file": Path(path).name, "sheet": book.active.title,
                                    "row": number, "headers": headers, "values": list(row)}))
        return result
    finally:
        book.close()


def import_history(mentor_path: str | Path, intern_path: str | Path) -> dict:
    mentors = read_workbook(mentor_path, "mentor")
    interns = read_workbook(intern_path, "intern")
    init_store()
    results = []
    with write_lock, connect() as db:
        # Establish only explicit mentor→intern mappings before reading intern surveys.
        for survey, _ in mentors:
            register_pair(db, survey.intern_id, survey.mentor_id, survey.department)
        for survey, raw in mentors + interns:
            event_id = "xlsx:" + digest(survey.model_dump(mode="json"))
            results.append(accept(db, survey, event_id, raw))
        pairs = db.execute("SELECT COUNT(*) FROM pair_v2_registry").fetchone()[0]
    return {"mentor_rows": len(mentors), "intern_rows": len(interns), "pairs": pairs,
            "accepted": sum(r["status"] == "accepted" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "months": sorted({s.month for s, _ in mentors + interns}),
            "missing_rating_cells": sum(len(RATINGS[s.role]) - len(s.ratings) for s, _ in mentors + interns),
            "missing_text_cells": sum(len(TEXTS[s.role]) - len(s.texts) for s, _ in mentors + interns)}
