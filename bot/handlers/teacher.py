import logging
import os
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.keyboards import lesson_action_keyboard, transfer_decision_keyboard
from bot.states import HomeworkStates, TransferStates

logger = logging.getLogger(__name__)
router = Router(name="teacher")

STATUS_PLANNED = int(os.getenv('STATUS_PLANNED', '1'))
STATUS_CANCELLED = int(os.getenv('STATUS_CANCELLED', '2'))
STATUS_CONDUCTED = int(os.getenv('STATUS_CONDUCTED', '3'))
MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]


class DateRangeStates(StatesGroup):
    waiting_for_date_from = State()
    waiting_for_date_to = State()


def _is_teacher(db: Database, telegram_id: int) -> bool:
    user = db.get_user(telegram_id)
    return bool(user and user["role"] == "teacher")


def _get_status_emoji(status: int) -> str:
    return {1: "📌", 2: "❌", 3: "✅"}.get(status, "❓")


def _get_status_text(status: int) -> str:
    return {1: "Запланирован", 2: "Отменён", 3: "Проведён"}.get(status, "Неизвестно")


async def _get_customer_name(alfacrm: AlfaCRMClient, customer_id: int) -> str:
    customer = await alfacrm.get_customer_info(customer_id)
    return alfacrm.extract_user_name(customer) if customer else f"ID:{customer_id}"


@router.message(F.text == "📅 Моё расписание")
async def teacher_schedule_menu(message: Message, state: FSMContext) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="📅 Неделя", callback_data="schedule:week")],
        [InlineKeyboardButton(text="📅 Месяц", callback_data="schedule:month")],
        [InlineKeyboardButton(text="📅 Свой период", callback_data="schedule:custom")],
    ])
    
    await message.answer("📅 <b>Выберите период:</b>", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "schedule:custom")
async def custom_date_from(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DateRangeStates.waiting_for_date_from)
    await callback.message.edit_text("📅 Введите начальную дату в формате <b>YYYY-MM-DD</b>\nПример: <code>2026-07-30</code>", parse_mode="HTML")
    await callback.answer()


@router.message(DateRangeStates.waiting_for_date_from, F.text)
async def custom_date_to(message: Message, state: FSMContext) -> None:
    date_from = message.text.strip()
    try:
        datetime.strptime(date_from, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату как <b>YYYY-MM-DD</b>", parse_mode="HTML")
        return
    
    await state.update_data(date_from=date_from)
    await state.set_state(DateRangeStates.waiting_for_date_to)
    await message.answer("📅 Введите конечную дату в формате <b>YYYY-MM-DD</b>", parse_mode="HTML")


@router.message(DateRangeStates.waiting_for_date_to, F.text)
async def show_custom_schedule(message: Message, state: FSMContext, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "teacher":
        await state.clear()
        return
    
    date_to = message.text.strip()
    try:
        datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату как <b>YYYY-MM-DD</b>", parse_mode="HTML")
        return
    
    data = await state.get_data()
    date_from = data["date_from"]
    await state.clear()
    
    await show_schedule(message, db, alfacrm, user, date_from, date_to)


@router.callback_query(F.data.startswith("schedule:"))
async def handle_schedule_period(callback: CallbackQuery, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(callback.from_user.id)
    if not user or user["role"] != "teacher":
        await callback.answer("❌ Только для преподавателей.", show_alert=True)
        return
    
    period = callback.data.split(":")[1]
    today = datetime.now().date()
    
    if period == "today":
        date_from = today.isoformat()
        date_to = today.isoformat()
    elif period == "tomorrow":
        tomorrow = today + timedelta(days=1)
        date_from = tomorrow.isoformat()
        date_to = tomorrow.isoformat()
    elif period == "week":
        date_from = today.isoformat()
        date_to = (today + timedelta(days=7)).isoformat()
    elif period == "month":
        date_from = today.isoformat()
        date_to = (today + timedelta(days=30)).isoformat()
    else:
        await callback.answer()
        return
    
    await callback.message.edit_text(f"🔍 Ищу уроки с <b>{date_from}</b> по <b>{date_to}</b>...", parse_mode="HTML")
    await callback.answer()
    await show_schedule(callback.message, db, alfacrm, user, date_from, date_to)


async def show_schedule(message: Message, db: Database, alfacrm: AlfaCRMClient, user, date_from: str, date_to: str):
    try:
        all_lessons = await alfacrm.get_lessons(
            teacher_id=user["crm_id"],
            status=1,
            date_from=date_from,
            date_to=date_to
        )
        
        if not all_lessons:
            await message.answer(
                f"📅 Нет уроков в период с <b>{date_from}</b> по <b>{date_to}</b>",
                parse_mode="HTML"
            )
            return
        
        await message.answer(
            f"📅 <b>Расписание</b> ({date_from} – {date_to})\nНайдено уроков: <b>{len(all_lessons)}</b>\n",
            parse_mode="HTML"
        )
        
        customer_names = {}
        
        for lesson in sorted(all_lessons, key=lambda l: l.get("time_from", "")):
            date = lesson.get("date", "—")
            time_from = lesson.get("time_from", "")[-8:-3] if lesson.get("time_from") else "??:??"
            time_to = lesson.get("time_to", "")[-8:-3] if lesson.get("time_to") else "??:??"
            status = lesson.get("status", 0)
            lesson_type = lesson.get("lesson_type_name", "Урок")
            topic = lesson.get("topic", "")
            
            customer_ids = lesson.get("customer_ids", [])
            names = []
            for cid in customer_ids:
                if cid not in customer_names:
                    customer_names[cid] = await _get_customer_name(alfacrm, cid)
                names.append(customer_names[cid])
            
            card = (
                f"{_get_status_emoji(status)} <b>{date}</b> | 🕐 {time_from} – {time_to}\n"
                f"👤 <b>{', '.join(names)}</b>\n"
                f"📊 {lesson_type} | {_get_status_text(status)}"
            )
            if topic:
                card += f"\n📝 {topic}"
            
            await message.answer(
                card,
                parse_mode="HTML",
                reply_markup=lesson_action_keyboard(lesson["id"])
            )
    
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка получения расписания: {e}")


@router.message(F.text == "📊 Отчёт по урокам")
async def teacher_report(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(message.from_user.id)
    if not user or user["role"] != "teacher":
        return

    date_from = (datetime.now().date() - timedelta(days=30)).isoformat()
    
    try:
        all_lessons = await alfacrm.get_lessons(
            teacher_id=user["crm_id"],
            date_from=date_from
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    total = len(all_lessons)
    conducted = sum(1 for l in all_lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in all_lessons if l.get("status") == STATUS_CANCELLED)
    not_closed = total - conducted - cancelled
    with_hw = sum(1 for l in all_lessons if l.get("homework") and l["homework"].strip())

    await message.answer(
        f"📊 <b>Отчёт за 30 дней</b>\n\n"
        f"📅 Всего: {total}\n"
        f"✅ Проведено: {conducted}\n"
        f"❌ Отменено: {cancelled}\n"
        f"⚠️ Не закрыто: {not_closed}\n\n"
        f"📚 С ДЗ: {with_hw}\n"
        f"📝 Без ДЗ: {conducted - with_hw}\n\n"
        f"{'⚠️ Есть незакрытые уроки!' if not_closed > 0 else '✅ Все уроки закрыты!'}",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("close:"))
async def close_lesson(callback: CallbackQuery, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = db.get_user(callback.from_user.id)
    if not user or user["role"] != "teacher":
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    
    try:
        await alfacrm.mark_lesson_conducted(lesson_id)
        db.mark_close_reminder_sent(lesson_id, callback.from_user.id)
        await callback.message.edit_text(f"{callback.message.text}\n\n✅ Проведён!")
        await callback.answer("✅ Урок проведён!")
    except AlfaCRMError as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith("hw:"))
async def attach_hw_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    user = db.get_user(callback.from_user.id)
    if not user or user["role"] != "teacher":
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(HomeworkStates.waiting_for_text_or_file)
    await callback.message.answer("📝 Отправьте текст или файл ДЗ.")
    await callback.answer()


@router.message(HomeworkStates.waiting_for_text_or_file, F.text)
async def attach_hw_text(message: Message, state: FSMContext, alfacrm: AlfaCRMClient) -> None:
    data = await state.get_data()
    try:
        await alfacrm.set_homework(data["lesson_id"], message.text)
        await message.answer("✅ ДЗ сохранено!")
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()


@router.message(HomeworkStates.waiting_for_text_or_file, F.document | F.photo)
async def attach_hw_file(message: Message, state: FSMContext, db: Database, alfacrm: AlfaCRMClient) -> None:
    data = await state.get_data()
    lesson_id = data["lesson_id"]

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "файл"
        file_type = "document"
    else:
        file_id = message.photo[-1].file_id
        file_name = "фото"
        file_type = "photo"

    db.add_homework_file(lesson_id, file_id, file_name, file_type)

    try:
        lesson = await alfacrm.get_lesson(lesson_id)
        existing_hw = (lesson or {}).get("homework", "")
        note = f"\n[📎 {file_name}]"
        await alfacrm.set_homework(lesson_id, f"{existing_hw}{note}" if existing_hw else note)
        await message.answer(f"✅ Файл прикреплён!")
    except AlfaCRMError as e:
        await message.answer(f"⚠️ Файл сохранён локально. Ошибка CRM: {e}")
    
    await state.clear()


@router.callback_query(F.data.startswith("transfer:"))
async def transfer_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    user = db.get_user(callback.from_user.id)
    if not user or user["role"] != "teacher":
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(TransferStates.waiting_for_comment)
    await callback.message.answer("🔁 Напишите желаемую дату/время и причину переноса.")
    await callback.answer()


@router.message(TransferStates.waiting_for_comment, F.text)
async def transfer_finish(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    request_id = db.create_transfer_request(message.from_user.id, data["lesson_id"], message.text)

    for manager_id in MANAGER_IDS:
        try:
            await message.bot.send_message(
                manager_id,
                f"🔁 Заявка на перенос #{request_id}\n"
                f"👨‍🏫 {message.from_user.full_name}\n"
                f"📅 Урок ID: {data['lesson_id']}\n"
                f"💬 {message.text}",
                reply_markup=transfer_decision_keyboard(request_id),
            )
        except Exception:
            pass

    await message.answer("✅ Заявка отправлена менеджеру.")
    await state.clear()