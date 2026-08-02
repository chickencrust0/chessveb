"""
bot/formatting.py — единый форматтер дат, времени и карточек уроков.

Все места вывода (сводки менеджера, расписание родителя и преподавателя,
рассылки планировщика) должны использовать функции отсюда, чтобы формат
не разъезжался.

Основные точки входа:
    build_summary(...)   -> list[str]   сводка менеджера, сгруппированная по дням
    build_schedule(...)  -> list[str]   расписание для роли parent/teacher/manager
    format_lesson(...)   -> str         одна карточка урока
    lesson_sort_key(...)                корректная сортировка (дата, время)
"""

from datetime import date, datetime, timedelta
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ==================== СЛОВАРИ ====================

WEEK_FULL = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
WEEK_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

STATUS_PLANNED = 1
STATUS_CANCELLED = 2
STATUS_CONDUCTED = 3

STATUS_LABELS = {
    STATUS_PLANNED: "📌 запланирован",
    STATUS_CANCELLED: "❌ отменён",
    STATUS_CONDUCTED: "✅ проведён",
}

TG_LIMIT = 4096
SAFE_LIMIT = 3900  # запас на теги и служебные строки
DIVIDER = "━━━━━━━━━━━━━━━━━━"


# ==================== ПАРСИНГ ====================

def _esc(value: Any) -> str:
    """Экранирует текст из CRM — он уходит в parse_mode=HTML."""
    return escape(str(value if value is not None else ""), quote=False)


def _parse_date_str(raw: Any) -> Optional[date]:
    """Понимает YYYY-MM-DD, DD.MM.YYYY и полный datetime в обоих вариантах."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("T", " ").split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_lesson_date(lesson: Dict[str, Any]) -> Optional[date]:
    """
    Дата урока. Сначала поле `date`, при неудаче — дата из `time_from`
    (AlfaCRM отдаёт его полным datetime).
    """
    return _parse_date_str(lesson.get("date")) or _parse_date_str(lesson.get("time_from"))


def lesson_date_iso(lesson: Dict[str, Any]) -> str:
    """
    Нормализованная дата в виде YYYY-MM-DD — для фильтрации сравнением строк.
    Заменяет небезопасные проверки вида `date_from <= l.get("date","") <= date_to`.
    """
    d = parse_lesson_date(lesson)
    return d.isoformat() if d else ""


def fmt_time(value: Any) -> str:
    """'2026-08-04 15:00:00' / '15:00:00' / '15:00' -> '15:00'."""
    s = str(value or "").strip()
    if not s:
        return "??:??"
    tail = s.replace("T", " ").split(" ")[-1]
    return tail[:5] if len(tail) >= 5 else "??:??"


def fmt_time_range(lesson: Dict[str, Any]) -> str:
    time_from = fmt_time(lesson.get("time_from"))
    time_to = fmt_time(lesson.get("time_to"))
    if time_to == "??:??":
        return time_from
    return f"{time_from}–{time_to}"


def lesson_sort_key(lesson: Dict[str, Any]) -> Tuple[str, str]:
    """Сортировка сначала по дате, затем по времени начала."""
    return (lesson_date_iso(lesson) or "9999-99-99", fmt_time(lesson.get("time_from")))


# ==================== ДАТЫ ====================

def fmt_date_short(d: Optional[date]) -> str:
    """04.08.2026, Пн"""
    if not d:
        return "дата не указана"
    return f"{d.strftime('%d.%m.%Y')}, {WEEK_SHORT[d.weekday()]}"


def fmt_date_long(d: Optional[date]) -> str:
    """4 августа, понедельник"""
    if not d:
        return "дата не указана"
    return f"{d.day} {MONTHS_GEN[d.month - 1]}, {WEEK_FULL[d.weekday()]}"


def relative_hint(d: Optional[date], today: Optional[date] = None) -> str:
    """'сегодня' / 'завтра' / 'вчера' — или пустая строка."""
    if not d:
        return ""
    today = today or datetime.now().date()
    delta = (d - today).days
    return {0: "сегодня", 1: "завтра", -1: "вчера"}.get(delta, "")


def day_header(d: Optional[date], today: Optional[date] = None) -> str:
    hint = relative_hint(d, today)
    text = fmt_date_long(d)
    if hint:
        text += f" — {hint}"
    return f"📅 <b>{_esc(text)}</b>"


def status_label(status: Any) -> str:
    try:
        return STATUS_LABELS.get(int(status), f"статус {status}")
    except (TypeError, ValueError):
        return "статус неизвестен"


# ==================== КАРТОЧКА УРОКА ====================

def format_lesson(
    lesson: Dict[str, Any],
    *,
    role: str = "manager",
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    hw_files_count: int = 0,
    show_date: bool = False,
    hw_limit: int = 120,
) -> str:
    """
    Карточка одного урока в виде строк «Метка: значение».

    role:
        "parent"  — дата, время, тип занятия, тема (без статуса и имени ребёнка)
        "teacher" — дата, время, ребёнок, статус, тема
        "manager" — дата, время, ребёнок, педагог, статус, ДЗ

    show_date=False — когда карточки уже сгруппированы под заголовком дня.
    """
    teachers = teachers or {}
    customers = customers or {}
    lines: List[str] = []

    if show_date:
        lines.append(f"📅 <b>Дата:</b> {_esc(fmt_date_short(parse_lesson_date(lesson)))}")

    lines.append(f"🕐 <b>Время:</b> {_esc(fmt_time_range(lesson))}")

    if role in ("teacher", "manager"):
        names = [customers.get(cid, f"ID:{cid}") for cid in lesson.get("customer_ids", [])]
        lines.append(f"👤 <b>Ребёнок:</b> {_esc(', '.join(names)) if names else '—'}")

    if role == "manager":
        names = [teachers.get(tid, f"ID:{tid}") for tid in lesson.get("teacher_ids", [])]
        lines.append(f"👨‍🏫 <b>Педагог:</b> {_esc(', '.join(names)) if names else '—'}")

    if role == "parent":
        lines.append(f"📘 <b>Занятие:</b> {_esc(lesson.get('lesson_type_name') or 'Урок')}")
    else:
        lines.append(f"📊 <b>Статус:</b> {_esc(status_label(lesson.get('status')))}")

    topic = (lesson.get("topic") or "").strip()
    if topic:
        lines.append(f"📝 <b>Тема:</b> {_esc(topic)}")

    if role in ("manager", "parent"):
        homework = (lesson.get("homework") or "").strip()
        if homework:
            if len(homework) > hw_limit:
                homework = homework[:hw_limit].rstrip() + "…"
            hw_line = f"📚 <b>ДЗ:</b> {_esc(homework)}"
            if hw_files_count:
                hw_line += f" (📎 {hw_files_count})"
            lines.append(hw_line)
        elif hw_files_count:
            lines.append(f"📎 <b>ДЗ:</b> файлов: {hw_files_count}")
        elif role == "manager":
            lines.append("📚 <b>ДЗ:</b> не указано")

    return "\n".join(lines)


# ==================== ГРУППИРОВКА И НАРЕЗКА ====================

def group_by_day(lessons: Iterable[Dict[str, Any]]) -> List[Tuple[Optional[date], List[Dict]]]:
    """[(дата, [уроки]), ...] в хронологическом порядке; уроки без даты — в конце."""
    buckets: Dict[str, List[Dict]] = {}
    for lesson in lessons:
        buckets.setdefault(lesson_date_iso(lesson), []).append(lesson)

    result: List[Tuple[Optional[date], List[Dict]]] = []
    for key in sorted(buckets, key=lambda k: k or "9999-99-99"):
        day_lessons = sorted(buckets[key], key=lambda l: fmt_time(l.get("time_from")))
        result.append((_parse_date_str(key), day_lessons))
    return result


def split_messages(blocks: Sequence[str], limit: int = SAFE_LIMIT) -> List[str]:
    """
    Склеивает блоки в сообщения не длиннее limit, не разрывая блок посередине
    (иначе ломаются HTML-теги).
    """
    messages: List[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        # блок сам по себе длиннее лимита — режем по строкам
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            messages.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        messages.append(current)
    return messages


def _day_counters(lessons: Sequence[Dict[str, Any]]) -> str:
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    planned = len(lessons) - conducted - cancelled
    parts = []
    if conducted:
        parts.append(f"✅ {conducted}")
    if cancelled:
        parts.append(f"❌ {cancelled}")
    if planned:
        parts.append(f"📌 {planned}")
    return "  ·  ".join(parts)


# ==================== ГОТОВЫЕ СООБЩЕНИЯ ====================

def build_summary(
    lessons: Sequence[Dict[str, Any]],
    *,
    period_label: str,
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    hw_counts: Optional[Dict[int, int]] = None,
    today: Optional[date] = None,
) -> List[str]:
    """
    Сводка менеджеру, сгруппированная по дням.
    Возвращает список готовых к отправке сообщений (parse_mode="HTML").

    hw_counts: {lesson_id: количество файлов ДЗ} — из db.get_homework_files().
    """
    hw_counts = hw_counts or {}
    today = today or datetime.now().date()

    if not lessons:
        return [f"📊 <b>Сводка {_esc(period_label)}</b>\n\nЗа этот период уроков не было."]

    total = len(lessons)
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    planned = total - conducted - cancelled

    blocks: List[str] = [
        f"📊 <b>Сводка {_esc(period_label)}</b>\n"
        f"Всего: {total}  ·  ✅ {conducted}  ·  ❌ {cancelled}  ·  📌 {planned}"
    ]

    for day, day_lessons in group_by_day(lessons):
        counters = _day_counters(day_lessons)
        header = f"{DIVIDER}\n{day_header(day, today)}"
        if counters:
            header += f"\n{counters}"
        header += f"\n{DIVIDER}"
        blocks.append(header)
        for lesson in day_lessons:
            blocks.append(format_lesson(
                lesson,
                role="manager",
                teachers=teachers,
                customers=customers,
                hw_files_count=hw_counts.get(lesson.get("id"), 0),
            ))

    return split_messages(blocks)


def build_schedule(
    lessons: Sequence[Dict[str, Any]],
    *,
    role: str,
    title: str = "Расписание",
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    empty_text: str = "На этот период занятий нет.",
    today: Optional[date] = None,
) -> List[str]:
    """Расписание с группировкой по дням. Возвращает список сообщений."""
    today = today or datetime.now().date()

    if not lessons:
        return [f"📅 <b>{_esc(title)}</b>\n\n{_esc(empty_text)}"]

    blocks: List[str] = [f"📅 <b>{_esc(title)}</b>\nВсего занятий: {len(lessons)}"]
    for day, day_lessons in group_by_day(lessons):
        blocks.append(f"{DIVIDER}\n{day_header(day, today)}\n{DIVIDER}")
        for lesson in day_lessons:
            blocks.append(format_lesson(
                lesson, role=role, teachers=teachers, customers=customers,
            ))
    return split_messages(blocks)


def format_reminder(
    lesson: Dict[str, Any],
    *,
    when: str,
    role: str,
    customers: Optional[Dict[int, str]] = None,
    today: Optional[date] = None,
) -> str:
    """Напоминание об уроке. Дата подставляется, только если урок не сегодня."""
    customers = customers or {}
    today = today or datetime.now().date()
    lesson_day = parse_lesson_date(lesson)

    head = "Урок" if role == "teacher" else "Занятие"
    lines = [f"⏰ <b>{head} через {_esc(when)}</b>", ""]

    if lesson_day and lesson_day != today:
        lines.append(f"📅 <b>Дата:</b> {_esc(fmt_date_short(lesson_day))}")
    lines.append(f"🕐 <b>Время:</b> {_esc(fmt_time_range(lesson))}")

    if role == "teacher":
        names = [customers.get(cid, f"ID:{cid}") for cid in lesson.get("customer_ids", [])]
        if names:
            lines.append(f"👤 <b>Ребёнок:</b> {_esc(', '.join(names))}")

    topic = (lesson.get("topic") or "").strip()
    if topic:
        lines.append(f"📝 <b>Тема:</b> {_esc(topic)}")

    return "\n".join(lines)
