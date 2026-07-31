import logging
import os
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.states import TransferStates

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
        all_lessons = await alfacrm.get_lessons(customer_id=user["crm_id"])
        lessons = [l for l in all_lessons if date_from <= l.get("date", "") <= date_to]
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if not lessons:
        await message.answer("📅 На неделю занятий нет.")
        return

    await message.answer(f"📅 <b>Расписание</b> ({len(lessons)} уроков):\n", parse_mode="HTML")
    
    for lesson in sorted(lessons, key=lambda l: l.get("time_from", "")):
        date = lesson.get("date", "—")
        time_from = lesson.get("time_from", "")[-8:-3] if lesson.get("time_from") else "??:??"
        time_to = lesson.get("time_to", "")[-8:-3] if lesson.get("time_to") else "??:??"
        status = alfacrm.get_lesson_status_label(lesson.get("status", 0))
        lesson_type = lesson.get("lesson_type_name", "Урок")
        topic = lesson.get("topic", "")
        
        card = f"📅 <b>{date}</b> | 🕐 {time_from}–{time_to}\n📊 {lesson_type} | {status}"
        if topic:
            card += f"\n📝 {topic}"
        
        await message.answer(card, parse_mode="HTML")


@router.message(F.text == "📚 Домашнее задание")
async def parent_homework(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    date_from = (datetime.now().date() - timedelta(days=14)).isoformat()
    
    try:
        all_lessons = await alfacrm.get_lessons(customer_id=user["crm_id"], status=STATUS_CONDUCTED)
        lessons_with_hw = [
            l for l in all_lessons
            if l.get("date", "") >= date_from
            and l.get("homework") and l["homework"].strip()
        ]
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if not lessons_with_hw:
        await message.answer("📚 Домашних заданий нет.")
        return

    await message.answer(f"📚 <b>Домашние задания</b> ({len(lessons_with_hw)}):\n", parse_mode="HTML")

    for lesson in sorted(lessons_with_hw, key=lambda l: l.get("date", ""), reverse=True):
        date = lesson.get("date", "")
        homework_text = lesson.get("homework", "")
        topic = lesson.get("topic", "")
        
        card = f"📅 <b>{date}</b>"
        if topic:
            card += f" | 📝 {topic}"
        card += f"\n{'─' * 30}\n{homework_text}"

        files = db.get_homework_files(lesson["id"])
        if files:
            card += f"\n\n📎 Файлы ({len(files)})"
        
        await message.answer(card, parse_mode="HTML")

        if files:
            for f in files:
                try:
                    if f.get("file_type") == "photo":
                        await message.answer_photo(f["file_id"])
                    else:
                        await message.answer_document(f["file_id"])
                except Exception:
                    pass


@router.message(F.text == "💰 Баланс")
async def parent_balance(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    try:
        customer = await alfacrm.get_customer_info(user["crm_id"])
        if customer:
            balance = customer.get("balance", "0")
            paid_count = customer.get("paid_count", 0)
            paid_lesson_count = customer.get("paid_lesson_count", 0)
            next_lesson = customer.get("next_lesson_date", "—")
            last_attend = customer.get("last_attend_date", "—")
            
            await message.answer(
                f"💰 <b>Абонемент</b>\n\n"
                f"💵 Баланс: {balance} руб.\n"
                f"📅 Оплачено занятий: {paid_lesson_count}\n"
                f"✅ Проведено: {paid_count}\n"
                f"📅 Следующее занятие: {next_lesson}\n"
                f"📅 Последнее посещение: {last_attend}",
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Не удалось получить данные.")
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(F.text == "🔁 Заявка на перенос")
async def parent_transfer_start(message: Message, state: FSMContext, db: Database) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "parent":
        return

    await state.set_state(TransferStates.waiting_for_comment)
    await message.answer(
        "🔁 <b>Заявка на перенос</b>\n\n"
        "Напишите дату, время урока и причину переноса.\n"
        "Заявка будет отправлена менеджеру.\n\n"
        "<i>Пример: 31.07.2026, 15:00, хотим перенести на 01.08</i>",
        parse_mode="HTML"
    )


@router.message(TransferStates.waiting_for_comment, F.text)
async def parent_transfer_send(message: Message, state: FSMContext, db: Database) -> None:
    user = db.get_user(message.from_user.id)
    comment = message.text
    
    # Уведомляем менеджеров
    for manager_id in MANAGER_IDS:
        try:
            await message.bot.send_message(
                manager_id,
                f"🔁 <b>Заявка на перенос от родителя</b>\n\n"
                f"👤 {user['full_name']}\n"
                f"🆔 CRM ID: {user['crm_id']}\n"
                f"📞 Телефон: {user['phone']}\n"
                f"💬 {comment}",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await message.answer("✅ Заявка отправлена менеджеру.")
    await state.clear()