"""Разбор файлов импорта: CSV/XLSX → длинный вид.

Контракт: docs/import-contract.md. Здесь только чтение файла, определение формата,
схлопывание широкого формата и книги с ролями к длинному виду и угадывание маппинга.
В базу ничего не пишется — это делает app.routers.imports.

Длинный вид — единственное представление, с которым работает commit:
строка = одна анкета (пара, роль, месяц, оценки, открытые ответы).
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .normalize import FACTORS, parse_role

SUPPORTED_SUFFIXES = frozenset({".csv", ".xlsx", ".xlsm"})
PREVIEW_SAMPLE_SIZE = 5

# Порядок важен: при равном счёте побеждает поле, которое идёт раньше.
# Например, «Наставник» — это имя наставника, а не фактор «поддержка наставника».
_FIELD_ORDER: tuple[str, ...] = (
    "month",
    "intern_name",
    "mentor_name",
    "role",
    "direction",
    *FACTORS,
    "q_better",
    "q_barrier",
)


class ImportFileError(Exception):
    """Базовая ошибка разбора файла. Роутер переводит её в 422."""


class UnsupportedFormatError(ImportFileError):
    """Расширение файла не поддержано. Роутер переводит её в 415."""


class UnreadableFileError(ImportFileError):
    """Файл пуст, битый или не содержит таблицы. Роутер переводит её в 422."""


@dataclass
class ParsedRow:
    """Строка длинного вида. `values` — значения по схлопнутым именам колонок."""

    number: int
    values: dict[str, Any]
    sheet: str | None = None
    role: str | None = None


@dataclass
class ParsedFile:
    """Результат разбора файла, ещё без учёта default_role."""

    name: str
    size: int
    sheets: list[str]
    format: str
    confidence: float
    columns: list[str]
    rows: list[ParsedRow]
    fields: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Текст и заголовки
# ---------------------------------------------------------------------------


def _norm(text: object) -> str:
    """Нижний регистр, ё→е, пунктуация→пробел, одиночные пробелы."""
    value = str(text).lower().replace("ё", "е").replace("\xa0", " ")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _norm_keep_len(text: object) -> str:
    """То же, но без схлопывания пробелов: позиции совпадают с исходной строкой.

    Нужно, чтобы вырезать из «Ясность M1» исходное «Ясность», сохранив регистр.
    """
    chars: list[str] = []
    for char in str(text).lower().replace("ё", "е").replace("\xa0", " "):
        chars.append(char if char.isalnum() else " ")
    return "".join(chars)


_RU_SUFFIXES = (
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими",
    "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ей", "ую", "юю",
    "ах", "ях", "ам", "ям", "ом", "ем", "ов", "ев", "ия", "ии", "ию",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь",
)


def _root(word: str) -> str:
    """Грубая основа слова: «мотивация» и «мотивации» → «мотивац»."""
    if len(word) > 3 and word.isascii() and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    for suffix in _RU_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _roots(text: object) -> list[str]:
    return [_root(word) for word in _norm(text).split()]


# Синонимы полей. Формы даются в нижнем регистре и без ё; сравнение идёт по основам,
# поэтому «Мотивация», «мотивации» и «мотивация сотрудника» сходятся к одному полю.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "intern_name": (
        "фио", "фио стажера", "фио сотрудника", "стажер", "стажеры",
        "имя стажера", "имя", "полное имя", "intern", "intern name", "trainee",
        "сотрудник", "участник", "респондент", "name",
    ),
    "mentor_name": (
        "наставник", "наставник фио", "фио наставника", "ментор", "ментор фио",
        "mentor", "mentor name", "куратор", "руководитель", "руководитель стажера",
    ),
    "direction": (
        "направление", "направление обучения", "направление стажировки",
        "direction", "отдел", "подразделение", "департамент",
        "специальность", "группа", "трек",
    ),
    "month": (
        "месяц", "месяц стажировки", "месяц программы", "period", "период",
        "month", "срок", "этап",
    ),
    "role": (
        "роль", "role", "тип анкеты", "тип", "кто заполнял",
        "respondent type", "категория", "тип респондента",
    ),
    "motivation": (
        "мотивация", "мотивация к работе", "вовлеченность", "интерес",
        "настрой", "желание работать", "motivation", "энергия", "драйв",
    ),
    "clarity": (
        "ясность", "ясность задач", "понятность задач", "понятные задачи",
        "определенность задач", "понимание задач", "clarity", "цели", "задачи",
    ),
    "mentor": (
        "поддержка наставника", "поддержка ментора", "поддержка",
        "оценка наставника", "качество наставничества", "наставник оценка",
        "менторство", "mentor support", "наставник",
    ),
    "workload": (
        "нагрузка", "рабочая нагрузка", "загруженность", "объем работы",
        "баланс нагрузки", "workload", "переработки",
    ),
    "inclusion": (
        "включенность", "включенность в команду", "отношения в команде",
        "команда", "коллектив", "принадлежность", "inclusion", "адаптация",
        "комфорт в команде",
    ),
    "future": (
        "будущее", "будущее в компании", "оффер", "перспектива", "перспективы",
        "планы", "намерение остаться", "желание остаться", "future",
        "карьера", "развитие",
    ),
    "q_better": (
        "что улучшилось", "что стало лучше", "что получилось", "что помогло",
        "сильные стороны", "плюсы", "улучшения", "improved", "what improved",
    ),
    "q_barrier": (
        "что мешает", "что мешало", "что улучшить", "трудности", "сложности",
        "барьеры", "проблемы", "замечания", "предложения", "пожелания",
        "комментарий", "коммент", "обратная связь", "дополнительно",
    ),
}

_SYNONYM_ITEMS: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {
    fieldname: tuple((_norm(syn), frozenset(_roots(syn))) for syn in synonyms)
    for fieldname, synonyms in _SYNONYMS.items()
}

_MENTOR_ROOTS = frozenset({"наставник", "ментор", "mentor", "куратор", "руководител"})
_INTERN_ROOTS = frozenset({"стажер", "intern", "trainee", "сотрудник", "участник"})


def _field_allowed(fieldname: str, header_norm: str) -> bool:
    """Не даём «ФИО наставника» уехать в имя стажёра и наоборот."""
    roots = set(_roots(header_norm))
    if fieldname == "intern_name":
        return not (roots & _MENTOR_ROOTS) or bool(roots & _INTERN_ROOTS)
    if fieldname == "mentor_name":
        return not (roots & _INTERN_ROOTS) or bool(roots & _MENTOR_ROOTS)
    return True


def _score_header(header_norm: str, fieldname: str) -> int:
    """0…100: насколько заголовок похож на поле."""
    items = _SYNONYM_ITEMS[fieldname]
    header_roots = set(_roots(header_norm))
    best = 0
    for syn_norm, syn_roots in items:
        if header_norm == syn_norm:
            return 100
        if syn_roots and syn_roots <= header_roots:
            # Чем больше слов в синониме, тем он специфичнее: «фио наставника» >
            # «фио» для заголовка «ФИО наставника».
            best = max(best, min(95, 60 + 5 * len(syn_roots)))
        if len(syn_norm) >= 5 and syn_norm in header_norm:
            best = max(best, 30)
    return best


def _header_field(header: object) -> str | None:
    """Лучшее поле для одного заголовка (без учёта занятости колонок)."""
    header_norm = _norm(header)
    if not header_norm:
        return None
    best_field: str | None = None
    best_score = 0
    for fieldname in _FIELD_ORDER:
        if not _field_allowed(fieldname, header_norm):
            continue
        score = _score_header(header_norm, fieldname)
        if score > best_score:
            best_score = score
            best_field = fieldname
    return best_field if best_score >= 60 else None


def _assign_fields(columns: list[str]) -> dict[str, str]:
    """Жадно раздаёт колонки полям: сначала точные совпадения, потом похожие."""
    candidates: list[tuple[int, int, int, str, str]] = []
    for index, column in enumerate(columns):
        header_norm = _norm(column)
        if not header_norm:
            continue
        for priority, fieldname in enumerate(_FIELD_ORDER):
            if not _field_allowed(fieldname, header_norm):
                continue
            score = _score_header(header_norm, fieldname)
            if score > 0:
                candidates.append((score, priority, index, fieldname, column))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

    assigned: dict[str, str] = {}
    used_columns: set[int] = set()
    for _score, _priority, index, fieldname, column in candidates:
        if fieldname in assigned or index in used_columns:
            continue
        assigned[fieldname] = column
        used_columns.add(index)
    return assigned


# ---------------------------------------------------------------------------
# Широкий формат: «Ясность M1», «Мотивация M2», …
# ---------------------------------------------------------------------------

_MONTH_SUFFIX_RE = re.compile(
    r"^\s*(?P<base>.+?)\s*(?:m|м|месяц|month)\s*[-_ ]?\s*(?P<month>[1-6])\s*$"
)


@dataclass
class _WideGroup:
    base: str
    factor: str
    by_month: dict[str, str] = field(default_factory=dict)


def _split_month_header(header: str) -> tuple[str, str, str] | None:
    """«Ясность M1» → («Ясность», «clarity», «M1»).

    Возвращает None, если суффикса нет или база — не фактор (например,
    «Наставник M1» — это, скорее, имя наставника по месяцам, а не оценка).
    """
    original = str(header)
    normalized = _norm_keep_len(original)
    match = _MONTH_SUFFIX_RE.match(normalized)
    if not match:
        return None
    base_norm = _norm(match.group("base"))
    factor = _header_field(base_norm)
    if factor not in FACTORS:
        return None
    base = original[: match.end("base")].strip().replace("\xa0", " ")
    base = re.sub(r"\s+", " ", base).strip()
    if not base:
        return None
    return base, factor, "M" + match.group("month")


def _wide_groups(columns: list[str]) -> dict[str, _WideGroup]:
    groups: dict[str, _WideGroup] = {}
    for column in columns:
        split = _split_month_header(column)
        if not split:
            continue
        base, factor, month = split
        key = _norm(base)
        group = groups.setdefault(key, _WideGroup(base=base, factor=factor))
        group.by_month.setdefault(month, column)
    return groups


def _detect_format(columns: list[str], fields: dict[str, str]) -> str:
    groups = _wide_groups(columns)
    if sum(len(group.by_month) for group in groups.values()) >= 2:
        return "wide"
    if fields.get("month"):
        return "long"
    return "unknown"


def _collapse_wide(
    columns: list[str],
    rows: list[ParsedRow],
    warnings: list[str],
    prefix: str = "",
) -> tuple[list[str], list[ParsedRow]]:
    """Размножает каждую строку по месяцам и оставляет базовые имена колонок."""
    groups = _wide_groups(columns)
    if not groups:
        return columns, rows

    column_to_group: dict[str, _WideGroup] = {}
    for group in groups.values():
        for column in group.by_month.values():
            column_to_group[column] = group
    suffixed = set(column_to_group)

    out_columns: list[str] = []
    for column in columns:
        if column in suffixed:
            group = column_to_group[column]
            if "Месяц" not in out_columns:
                out_columns.append("Месяц")
            if group.base not in out_columns:
                out_columns.append(group.base)
        elif column not in out_columns:
            out_columns.append(column)

    plain = [column for column in columns if column not in suffixed]
    months = sorted(
        {month for group in groups.values() for month in group.by_month},
        key=lambda value: int(value[1:]),
    )

    out_rows: list[ParsedRow] = []
    dropped = 0
    for row in rows:
        row_months = [
            month
            for month in months
            if any(
                _has_value(row.values.get(group.by_month[month]))
                for group in groups.values()
                if month in group.by_month
            )
        ]
        if not row_months:
            dropped += 1
            continue
        for month in row_months:
            values: dict[str, Any] = {}
            for column in plain:
                value = row.values.get(column)
                if _has_value(value):
                    values[column] = value
            values["Месяц"] = month
            for group in groups.values():
                column = group.by_month.get(month)
                if not column:
                    continue
                value = row.values.get(column)
                if _has_value(value):
                    values[group.base] = value
            out_rows.append(
                ParsedRow(
                    number=row.number,
                    values=values,
                    sheet=row.sheet,
                    role=row.role,
                )
            )

    if dropped:
        warnings.append(f"{prefix}пропущено строк без оценок по месяцам: {dropped}")
    warnings.append(
        f"{prefix}широкий формат: {len(months)} мес. "
        f"({', '.join(months)}) схлопнуты в длинный вид"
    )
    return out_columns, out_rows


# ---------------------------------------------------------------------------
# Чтение CSV и XLSX
# ---------------------------------------------------------------------------


def _decode_csv(content: bytes) -> tuple[str, str]:
    """UTF-8 BOM → UTF-8 → cp1251. Кодировку определяем по BOM и ошибке декодирования."""
    if content.startswith(b"\xef\xbb\xbf"):
        try:
            return content.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return content.decode("cp1251"), "cp1251"
        except UnicodeDecodeError as exc:  # теоретически недостижимо
            raise UnreadableFileError(
                "не удалось определить кодировку CSV: ожидается UTF-8 или cp1251"
            ) from exc


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join([line for line in text.splitlines() if line.strip()][:10])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample else ""
        counts = {delimiter: first_line.count(delimiter) for delimiter in (",", ";", "\t")}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def _read_csv_rows(content: bytes) -> tuple[list[list[Any]], list[str]]:
    text, _encoding = _decode_csv(content)
    delimiter = _sniff_delimiter(text)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise UnreadableFileError(f"CSV не читается: {exc}") from exc
    return rows, []


def _read_workbook_rows(
    content: bytes,
) -> tuple[list[str], dict[str, list[list[Any]]]]:
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl кидает разные типы
        raise UnreadableFileError(f"не удалось открыть книгу Excel: {exc}") from exc
    try:
        raw: dict[str, list[list[Any]]] = {}
        for worksheet in workbook.worksheets:
            raw[worksheet.title] = [
                list(row) for row in worksheet.iter_rows(values_only=True)
            ]
        sheets = list(raw)
    finally:
        workbook.close()
    return sheets, raw


def _clean_header(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)


def _clean_value(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.replace("\ufeff", "").replace("\xa0", " ").strip()
        return text or None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _has_value(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _max_width(rows: list[list[Any]]) -> int:
    return max((len(row) for row in rows), default=0)


def _find_header(
    raw_rows: list[list[Any]],
    warnings: list[str],
    prefix: str = "",
) -> tuple[int | None, list[str]]:
    """Первая строка, похожая на заголовок. Понимает шапку не в первой строке."""
    limit = min(len(raw_rows), 15)
    for index in range(limit):
        headers = [_clean_header(cell) for cell in raw_rows[index]]
        if len([header for header in headers if header]) < 2:
            continue
        score = 0
        for header in headers:
            if not header:
                continue
            if _split_month_header(header):
                score += 1
                continue
            fieldname = _header_field(header)
            if fieldname == "month":
                score += 3
            elif fieldname == "intern_name":
                score += 2
            elif fieldname:
                score += 1
        if score >= 3:
            if index > 0:
                warnings.append(
                    f"{prefix}заголовок найден в строке {index + 1}, "
                    f"строки выше пропущены"
                )
            return index, headers

    # Фолбэк: раз формат всё равно unknown, дадим пользователю увидеть колонки.
    for index, row in enumerate(raw_rows):
        headers = [_clean_header(cell) for cell in row]
        if len([header for header in headers if header]) >= 2:
            warnings.append(f"{prefix}заголовок не распознан — взята первая непустая строка")
            return index, headers
    return None, []


def _prepare_columns(
    headers: list[str],
    width: int,
    warnings: list[str],
    prefix: str = "",
) -> tuple[list[str], list[int]]:
    columns: list[str] = []
    indexes: list[int] = []
    seen: dict[str, int] = {}
    for index in range(width):
        name = headers[index] if index < len(headers) else ""
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            unique = f"{name} ({seen[name]})"
            warnings.append(f"{prefix}повторяющийся заголовок «{name}» переименован в «{unique}»")
            name = unique
        columns.append(name)
        indexes.append(index)
    return columns, indexes


def _build_rows(
    raw_rows: list[list[Any]],
    header_index: int,
    columns: list[str],
    indexes: list[int],
    warnings: list[str],
    prefix: str = "",
    sheet: str | None = None,
) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    pending_empty = 0
    for offset, raw in enumerate(raw_rows[header_index + 1 :]):
        values: dict[str, Any] = {}
        for column, index in zip(columns, indexes):
            value = _clean_value(raw[index] if index < len(raw) else None)
            if _has_value(value):
                values[column] = value
        if not values:
            pending_empty += 1
            continue
        if pending_empty:
            warnings.append(f"{prefix}пропущено пустых строк: {pending_empty}")
            pending_empty = 0
        rows.append(
            ParsedRow(
                number=header_index + 2 + offset,
                values=values,
                sheet=sheet,
            )
        )
    return rows


def _rows_have_data(rows: list[list[Any]]) -> bool:
    return any(
        _has_value(_clean_value(cell))
        for row in rows
        for cell in row
    )


def _table_from_sheet(
    title: str,
    raw_rows: list[list[Any]],
    warnings: list[str],
) -> tuple[list[str], list[ParsedRow]] | None:
    prefix = f"лист «{title}»: "
    header_index, headers = _find_header(raw_rows, warnings, prefix)
    if header_index is None:
        return None
    width = _max_width(raw_rows)
    columns, indexes = _prepare_columns(headers, width, warnings, prefix)
    if not columns:
        warnings.append(f"{prefix}не найдено ни одной именованной колонки")
        return None
    rows = _build_rows(
        raw_rows,
        header_index,
        columns,
        indexes,
        warnings,
        prefix,
        sheet=title,
    )
    if not rows:
        warnings.append(f"{prefix}нет строк данных")
        return None
    return columns, rows


# ---------------------------------------------------------------------------
# Сборка ParsedFile
# ---------------------------------------------------------------------------


def _confidence_long(fields: dict[str, str]) -> float:
    coverage = sum(1 for factor in FACTORS if fields.get(factor)) / len(FACTORS)
    value = 0.5
    value += 0.15 if fields.get("month") else 0.0
    value += 0.15 if fields.get("intern_name") else 0.0
    value += 0.12 * coverage
    value += 0.04 if fields.get("mentor_name") or fields.get("direction") else 0.0
    value += 0.03 if fields.get("q_better") or fields.get("q_barrier") else 0.0
    return round(min(0.97, value), 2)


def _confidence_wide(columns: list[str], fields: dict[str, str]) -> float:
    groups = _wide_groups(columns)
    month_count = len({month for group in groups.values() for month in group.by_month})
    coverage = len(groups) / len(FACTORS)
    value = 0.5
    value += 0.1 if fields.get("intern_name") else 0.0
    # Месяц в широком формате закодирован в заголовках — он есть по определению.
    value += 0.08 if groups else 0.0
    value += 0.15 * min(len(groups), 3) / 3
    value += 0.07 if month_count >= 2 else 0.0
    value += 0.05 * coverage
    return round(min(0.97, value), 2)


def _confidence_two_sheet(fields: dict[str, str], role_sheets: int) -> float:
    coverage = sum(1 for factor in FACTORS if fields.get(factor)) / len(FACTORS)
    value = 0.55
    value += 0.12 if role_sheets >= 2 else 0.05
    value += 0.1 if fields.get("intern_name") else 0.0
    value += 0.1 if fields.get("month") else 0.0
    value += 0.1 * coverage
    value += 0.05 if fields.get("q_better") or fields.get("q_barrier") else 0.0
    return round(min(0.97, value), 2)


def _make_parsed(
    name: str,
    size: int,
    sheets: list[str],
    format_name: str,
    columns: list[str],
    rows: list[ParsedRow],
    warnings: list[str],
    role_sheets: int = 0,
    confidence: float | None = None,
) -> ParsedFile:
    fields = _assign_fields(columns)
    if confidence is None:
        if format_name == "long":
            confidence = _confidence_long(fields)
        elif format_name == "wide":
            confidence = _confidence_wide(columns, fields)
        elif format_name == "two_sheet":
            confidence = _confidence_two_sheet(fields, role_sheets)
        else:
            confidence = round(0.15 + (0.05 if fields else 0.0), 2)
    return ParsedFile(
        name=name,
        size=size,
        sheets=sheets,
        format=format_name,
        confidence=confidence,
        columns=columns,
        rows=rows,
        fields=fields,
        warnings=warnings,
    )


def _parse_csv_file(name: str, content: bytes) -> ParsedFile:
    warnings: list[str] = []
    raw_rows, _ = _read_csv_rows(content)
    if not raw_rows:
        raise UnreadableFileError("файл пуст")
    header_index, headers = _find_header(raw_rows, warnings)
    if header_index is None:
        raise UnreadableFileError("не удалось найти строку заголовка")
    width = _max_width(raw_rows)
    columns, indexes = _prepare_columns(headers, width, warnings)
    if not columns:
        raise UnreadableFileError("в файле нет именованных колонок")
    rows = _build_rows(raw_rows, header_index, columns, indexes, warnings)
    if not rows:
        raise UnreadableFileError("файл не содержит строк данных")

    fields = _assign_fields(columns)
    format_name = _detect_format(columns, fields)
    confidence = _confidence_wide(columns, fields) if format_name == "wide" else None
    if format_name == "wide":
        columns, rows = _collapse_wide(columns, rows, warnings)
    return _make_parsed(
        name=name,
        size=len(content),
        sheets=[],
        format_name=format_name,
        columns=columns,
        rows=rows,
        warnings=warnings,
        confidence=confidence,
    )


def _parse_workbook_file(name: str, content: bytes, sheet: str | None) -> ParsedFile:
    warnings: list[str] = []
    sheets, raw = _read_workbook_rows(content)
    non_empty = [title for title in sheets if _rows_have_data(raw[title])]
    if not non_empty:
        raise UnreadableFileError("книга не содержит данных")

    explicit = (sheet or "").strip() or None
    if explicit is not None and explicit not in sheets:
        raise UnreadableFileError(f"лист «{explicit}» не найден в книге")

    selected: list[str]
    two_sheet_mode = False
    if explicit is not None:
        if explicit not in non_empty:
            raise UnreadableFileError(f"лист «{explicit}» пуст")
        selected = [explicit]
        two_sheet_mode = parse_role(explicit) is not None
    else:
        role_sheets = [title for title in non_empty if parse_role(title)]
        if role_sheets:
            two_sheet_mode = True
            selected = role_sheets
            ignored = [title for title in non_empty if title not in role_sheets]
            if ignored:
                warnings.append(
                    "листы без роли пропущены: "
                    + ", ".join(f"«{title}»" for title in ignored)
                )
        else:
            selected = [non_empty[0]]

    tables: list[tuple[str, list[str], list[ParsedRow], str, float | None]] = []
    for title in selected:
        table = _table_from_sheet(title, raw[title], warnings)
        if table is None:
            continue
        columns, rows = table
        fields = _assign_fields(columns)
        sheet_format = _detect_format(columns, fields)
        confidence = (
            _confidence_wide(columns, fields) if sheet_format == "wide" else None
        )
        if sheet_format == "wide":
            columns, rows = _collapse_wide(columns, rows, warnings, f"лист «{title}»: ")
        tables.append((title, columns, rows, sheet_format, confidence))

    if not tables:
        raise UnreadableFileError("в книге нет данных для импорта")

    if two_sheet_mode:
        columns: list[str] = []
        for _title, sheet_columns, _rows, _format, _confidence in tables:
            for column in sheet_columns:
                if column not in columns:
                    columns.append(column)
        if "Роль" not in columns:
            columns.append("Роль")

        rows: list[ParsedRow] = []
        for title, _columns, sheet_rows, _format, _confidence in tables:
            role = parse_role(title) or "intern"
            for row in sheet_rows:
                row.role = role
                row.values["Роль"] = role
                rows.append(row)

        if len(tables) > 1:
            warnings.append(
                "листы объединены: "
                + ", ".join(f"«{title}»" for title, _, _, _, _ in tables)
            )
        return _make_parsed(
            name=name,
            size=len(content),
            sheets=sheets,
            format_name="two_sheet",
            columns=columns,
            rows=rows,
            warnings=warnings,
            role_sheets=len(tables),
        )

    _title, columns, rows, format_name, confidence = tables[0]
    return _make_parsed(
        name=name,
        size=len(content),
        sheets=sheets,
        format_name=format_name,
        columns=columns,
        rows=rows,
        warnings=warnings,
        confidence=confidence,
    )


def parse_file(name: str, content: bytes, sheet: str | None = None) -> ParsedFile:
    """Читает файл и приводит его к длинному виду. В БД ничего не пишет."""
    file_name = name or ""
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFormatError("поддерживаются только .csv, .xlsx и .xlsm")
    if not content:
        raise UnreadableFileError("файл пуст")
    if suffix == ".csv":
        return _parse_csv_file(file_name, content)
    return _parse_workbook_file(file_name, content, sheet)


# ---------------------------------------------------------------------------
# Маппинг
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    1: "одному",
    2: "двум",
    3: "трём",
    4: "четырём",
    5: "пяти",
    6: "шести",
}


def _missing_factors_text(missing: list[str]) -> str:
    count = len(missing)
    word = _NUMBER_WORDS.get(count, str(count))
    if count == 1:
        return "оценка по одному фактору не найдена — она будет помечена как отсутствующая"
    return (
        f"оценки по {word} факторам не найдены — "
        "они будут помечены как отсутствующие"
    )


def _valid_role(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in ("intern", "mentor") else None


def guess_mapping(
    parsed: ParsedFile,
    default_role: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Предложение маппинга и осмысленные предупреждения для preview.

    `mapping` всегда описывает длинный вид, даже если файл был широким.
    """
    fields = parsed.fields
    role_default = _valid_role(default_role) or "intern"

    mapping: dict[str, Any] = {
        "intern_name": fields.get("intern_name"),
        "mentor_name": fields.get("mentor_name"),
        "direction": fields.get("direction"),
        "month": fields.get("month"),
        "role": fields.get("role"),
        "default_role": role_default,
        "factors": {factor: fields.get(factor) for factor in FACTORS},
        "open": {
            "q_better": fields.get("q_better"),
            "q_barrier": fields.get("q_barrier"),
        },
    }

    warnings = list(parsed.warnings)
    if not fields.get("intern_name"):
        warnings.append(
            "не найдена колонка с именем стажёра — такие строки уйдут в unmatched"
        )
    if not fields.get("month"):
        warnings.append(
            "не найдена колонка месяца — строки без месяца нельзя импортировать"
        )
    missing_factors = [factor for factor in FACTORS if not fields.get(factor)]
    if missing_factors:
        warnings.append(_missing_factors_text(missing_factors))
    if parsed.format == "two_sheet":
        warnings.append(
            "роль определена по имени листа — default_role к таким строкам не применяется"
        )
    elif not fields.get("role"):
        warnings.append(
            f"роль в файле не найдена — будет использована роль по умолчанию: {role_default}"
        )
    if parsed.format == "unknown":
        warnings.append("формат не распознан — проверьте маппинг вручную")

    recognized = {value for value in fields.values() if value}
    unrecognized = [column for column in parsed.columns if column not in recognized]
    if unrecognized:
        tail = ", ".join(f"«{column}»" for column in unrecognized[:6])
        if len(unrecognized) > 6:
            tail += f" и ещё {len(unrecognized) - 6}"
        warnings.append(f"не распознаны колонки: {tail}")

    unique_warnings = list(dict.fromkeys(warnings))
    return mapping, unique_warnings
