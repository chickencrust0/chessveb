import logging
from typing import List, Dict, Any

from alfacrm_client import AlfaCRMClient
from database import Database

logger = logging.getLogger(__name__)


async def get_lesson_summary(
    lessons: List[Dict[str, Any]],
    db: Database,
    alfacrm: AlfaCRMClient,
    period_label: str
) -> str:
    """
    Формирует текст сводки по урокам для менеджера.
    """
    if not lessons:
        logger.info(f"⚠️ Нет уроков для периода {period_label}")
        return f"📊 <b>Сводка {period_label}</b>\n\nЗа этот период уроков не было."

    # Получаем имена преподавателей
    try:
        teacher_response = await alfacrm._make_request("POST", "/v2api/1/teacher/index", json={"page": 0})
        teachers = {t['id']: t.get('name', 'Без имени') for t in teacher_response.get('items', [])}
    except Exception as e:
        logger.error(f"Ошибка получения преподавателей: {e}")
        teachers = {}

    # Получаем имена учеников (клиентов)
    try:
        customers = await alfacrm.load_all_customers(is_study=1)
        customers_dict = {c['id']: c.get('name', 'Без имени') for c in customers}
    except Exception as e:
        logger.error(f"Ошибка получения клиентов: {e}")
        customers_dict = {}

    total = len(lessons)
    conducted = sum(1 for l in lessons if l.get('status') == 3)   # проведён
    cancelled = sum(1 for l in lessons if l.get('status') == 2)   # отменён
    planned = total - conducted - cancelled                       # запланирован

    lines = [f"📊 <b>Сводка {period_label}</b>:"]
    lines.append(f"Всего уроков: {total}")
    lines.append(f"✅ Проведено: {conducted}")
    if cancelled:
        lines.append(f"❌ Отменено: {cancelled}")
    if planned:
        lines.append(f"📌 Запланировано: {planned}")
    lines.append("")

    if lessons:
        lines.append("Детали:")
        for lesson in sorted(lessons, key=lambda l: l.get('time_from', '')):
            time_from = lesson.get('time_from', '')[-8:-3] if lesson.get('time_from') else '??:??'
            teacher_names = [teachers.get(tid, f"ID:{tid}") for tid in lesson.get('teacher_ids', [])]
            teacher_str = ', '.join(teacher_names) if teacher_names else 'Не указан'
            customer_names = [customers_dict.get(cid, f"ID:{cid}") for cid in lesson.get('customer_ids', [])]
            customer_str = ', '.join(customer_names) if customer_names else 'Не указан'

            # Определяем статус с эмодзи
            status = lesson.get('status')
            if status == 3:
                status_label = "✅ Проведен"
            elif status == 2:
                status_label = "❌ Отменён"
            else:
                status_label = "📌 Запланирован"

            lines.append(f"🕐 {time_from} | {customer_str} | {teacher_str} | {status_label}")

            # Домашнее задание
            homework_raw = lesson.get('homework')
            homework_text = homework_raw.strip() if homework_raw else ''
            files = db.get_homework_files(lesson['id'])
            if homework_text:
                if len(homework_text) > 100:
                    homework_text = homework_text[:100] + '...'
                line = f"   📚 ДЗ: {homework_text}"
                if files:
                    line += f" (➕ {len(files)} файлов)"
                lines.append(line)
            else:
                if files:
                    lines.append(f"   📎 Есть файлы ДЗ ({len(files)})")
                else:
                    lines.append("   📚 ДЗ: не указано")
    else:
        lines.append("За этот период уроков не было.")

    return "\n".join(lines)