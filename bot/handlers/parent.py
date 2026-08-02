import logging
import os
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.states import TransferStates
from bot.formatting import (
    STATUS_CANCELLED,
    build_schedule,
    fmt_date_short,
    lesson_date_iso,
    parse_lesson_date,
    relative_hint,
)
from bot.handlers.common import answer_blocks

logger = logging.getLogger(__name__)
router = Router(name="parent")

STATUS_CONDUCTED = int(os.getenv('STATUS_CONDUCTED', '3'))
MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]


@router.message(F.text == "📅 Расписание")
async def parent_schedule(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    today = datetime.now().date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=7)).isoformat()

    try:
        all_lessons = await alfacrm.get_lessons(
            customer_id=user["crm_id"],
            date_from=date_from,
            date_to=date_to,
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    # Нормализуем дату перед сравнением: CRM может отдать как YYYY-MM-DD,
    # так и DD.MM.YYYY — строковое сравнение «как есть» на этом ломается.
    lessons = [
        l for l in all_lessons
        if date_from <= lesson_date_iso(l) <= date_to
        and l.get("status") != STATUS_CANCELLED  # отменённые родителю не показываем
    ]

    blocks = build_schedule(
        lessons,
        role="parent",
        title="Расписание на неделю",
        empty_text="На ближайшую неделю занятий нет.",
    )
    await answer_blocks(message, blocks)


@router.message(F.text == "📚 Домашнее задание")
async def parent_homework(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    date_from = (datetime.now().date() - timedelta(days=14)).isoformat()

    try:
        all_lessons = await alfacrm.get_lessons(
            customer_id=user["crm_id"], status=STATUS_CONDUCTED
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    lessons_with_hw = [
        l for l in all_lessons
        if lesson_date_iso(l) >= date_from
        and l.get("homework") and l["homework"].strip()
    ]

    if not lessons_with_hw:
        await message.answer("📚 Домашних заданий за последние 2 недели нет.")
        return

    await message.answer(
        f"📚 <b>Домашние задания</b> ({len(lessons_with_hw)})",
        parse_mode="HTML",
    )

    # Сортируем по нормализованной дате, свежие — сверху.
    for lesson in sorted(lessons_with_hw, key=lesson_date_iso, reverse=True):
        day = parse_lesson_date(lesson)
        hint = relative_hint(day)
        date_line = fmt_date_short(day) + (f" — {hint}" if hint else "")

        card = f"📅 <b>Дата:</b> {date_line}"
        topic = (lesson.get("topic") or "").strip()
        if topic:
            card += f"\n📝 <b>Тема:</b> {topic}"
        card += f"\n\n{lesson.get('homework', '')}"

        files = db.get_homework_files(lesson["id"])
        if files:
            card += f"\n\n📎 Файлов: {len(files)}"

        await message.answer(card, parse_mode="HTML")

        for f in files:
            try:
                if f.get("file_type") == "photo":
                    await message.answer_photo(f["file_id"])
                else:
                    await message.answer_document(f["file_id"])
            except Exception as e:
                logger.warning(f"Не удалось отправить файл ДЗ {f.get('file_id')}: {e}")


@router.message(F.text == "💰 Баланс")
async def parent_balance(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    try:
        customer = await alfacrm.get_customer_info(user["crm_id"])
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if not customer:
        await message.answer("❌ Не удалось получить данные.")
        return

    await message.answer(
        f"💰 <b>Абонемент</b>\n\n"
        f"💵 <b>Баланс:</b> {customer.get('balance', '0')} руб.\n"
        f"📅 <b>Оплачено занятий:</b> {customer.get('paid_lesson_count', 0)}\n"
        f"✅ <b>Проведено:</b> {customer.get('paid_count', 0)}\n"
        f"➡️ <b>Следующее занятие:</b> {customer.get('next_lesson_date') or '—'}\n"
        f"⬅️ <b>Последнее посещение:</b> {customer.get('last_attend_date') or '—'}",
        parse_mode="HTML",
    )


@router.message(F.text == "🔁 Заявка на перенос")
async def parent_transfer_start(message: Message, state: FSMContext, db: Database) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    await state.set_state(TransferStates.waiting_for_comment)
    example_date = (datetime.now().date() + timedelta(days=1)).strftime("%d.%m.%Y")
    await message.answer(
        "🔁 <b>Заявка на перенос</b>\n\n"
        "Напишите дату, время урока и причину переноса.\n"
        "Заявка будет отправлена менеджеру.\n\n"
        f"<i>Пример: {example_date}, 15:00, хотим перенести на следующий день</i>",
        parse_mode="HTML",
    )


@router.message(TransferStates.waiting_for_comment, F.text)
async def parent_transfer_send(message: Message, state: FSMContext, db: Database) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    comment = message.text
    sent_at = datetime.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in MANAGER_IDS:
        try:
            await message.bot.send_message(
                manager_id,
                f"🔁 <b>Заявка на перенос от родителя</b>\n\n"
                f"📅 <b>Получена:</b> {sent_at}\n"
                f"👤 <b>Родитель:</b> {user['full_name']}\n"
                f"🆔 <b>CRM ID:</b> {user['crm_id']}\n"
                f"📞 <b>Телефон:</b> {user['phone']}\n"
                f"💬 <b>Комментарий:</b> {comment}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
    await state.clear()
