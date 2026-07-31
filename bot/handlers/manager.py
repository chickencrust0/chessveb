import logging
import os
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import Database
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from bot.keyboards import transfer_decision_keyboard
from bot.states import BroadcastStates, ManagerSummaryStates
from bot.handlers.common import get_lesson_summary

logger = logging.getLogger(__name__)
router = Router(name="manager")

MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]
BRANCH_ID = os.getenv('BRANCH_ID', '1')


def _is_manager(telegram_id: int) -> bool:
    return telegram_id in MANAGER_IDS


# ==================== РАССЫЛКА ====================

@router.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not _is_manager(message.from_user.id):
        return
    await state.set_state(BroadcastStates.waiting_for_text)
    await message.answer("📢 Введите текст рассылки.")


@router.message(BroadcastStates.waiting_for_text, F.text)
async def broadcast_choose(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍🏫 Преподавателям", callback_data="broadcast:teacher")],
        [InlineKeyboardButton(text="👨‍👩‍👧 Родителям", callback_data="broadcast:parent")],
        [InlineKeyboardButton(text="👥 Всем", callback_data="broadcast:all")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])
    await message.answer("Кому отправить?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_execute(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        return
    action = callback.data.split(":")[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    recipients = []
    if action == "teacher":
        recipients = db.get_all_users_by_role("teacher")
    elif action == "parent":
        recipients = db.get_all_users_by_role("parent")
    else:
        recipients = db.get_all_users_by_role("teacher") + db.get_all_users_by_role("parent")
    success = 0
    for user in recipients:
        try:
            await callback.bot.send_message(user["telegram_id"], f"📢 {text}")
            success += 1
        except Exception:
            pass
    await callback.message.edit_text(f"✅ Отправлено: {success}/{len(recipients)}")
    await state.clear()


# ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

@router.message(F.text == "🔁 Заявки на перенос")
async def transfer_list(message: Message, db: Database) -> None:
    if not _is_manager(message.from_user.id):
        return
    requests = db.get_pending_transfer_requests()
    if not requests:
        await message.answer("📭 Заявок нет.")
        return
    for req in requests:
        await message.answer(
            f"🔁 Заявка №{req['id']}\n"
            f"👤 {req.get('teacher_name', '—')}\n"
            f"💬 {req.get('comment', '—')}",
            reply_markup=transfer_decision_keyboard(req['id'])
        )


@router.callback_query(F.data.startswith("transfer_ok:"))
async def transfer_approve(callback: CallbackQuery, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        return
    request_id = int(callback.data.split(":")[1])
    request = db.get_transfer_request(request_id)
    if request:
        db.resolve_transfer_request(request_id, "approved", callback.from_user.id)
        try:
            await callback.bot.send_message(request["teacher_telegram_id"], f"✅ Заявка №{request_id} одобрена!")
        except Exception:
            pass
    await callback.message.edit_text(f"✅ Заявка №{request_id} одобрена.")
    await callback.answer()


@router.callback_query(F.data.startswith("transfer_no:"))
async def transfer_reject(callback: CallbackQuery, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        return
    request_id = int(callback.data.split(":")[1])
    request = db.get_transfer_request(request_id)
    if request:
        db.resolve_transfer_request(request_id, "rejected", callback.from_user.id)
        try:
            await callback.bot.send_message(request["teacher_telegram_id"], f"❌ Заявка №{request_id} отклонена.")
        except Exception:
            pass
    await callback.message.edit_text(f"❌ Заявка №{request_id} отклонена.")
    await callback.answer()


# ==================== СВОДКА ЗА ПЕРИОД ====================

@router.message(F.text == "📊 Сводка за период")
async def summary_period_start(message: Message, state: FSMContext) -> None:
    if not _is_manager(message.from_user.id):
        return
    logger.info(f"🔔 Менеджер {message.from_user.id} запросил сводку за период")
    await state.set_state(ManagerSummaryStates.waiting_for_date_from)
    logger.info(f"Состояние установлено: {await state.get_state()}")
    await message.answer(
        "📅 Введите начальную дату в формате <b>YYYY-MM-DD</b>\n"
        "Пример: <code>2026-07-30</code>",
        parse_mode="HTML"
    )


@router.message(ManagerSummaryStates.waiting_for_date_from, F.text)
async def summary_date_from(message: Message, state: FSMContext) -> None:
    logger.info(f"📅 ХЕНДЛЕР СРАБОТАЛ! Введена начальная дата: {message.text}")
    date_from = message.text.strip()
    try:
        datetime.strptime(date_from, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату как <b>YYYY-MM-DD</b>", parse_mode="HTML")
        return
    await state.update_data(date_from=date_from)
    await state.set_state(ManagerSummaryStates.waiting_for_date_to)
    logger.info(f"Переход к вводу конечной даты, состояние: {await state.get_state()}")
    await message.answer("📅 Введите конечную дату в формате <b>YYYY-MM-DD</b>", parse_mode="HTML")


@router.message(ManagerSummaryStates.waiting_for_date_to, F.text)
async def summary_date_to(message: Message, state: FSMContext, db: Database, alfacrm: AlfaCRMClient) -> None:
    logger.info(f"📅 ХЕНДЛЕР СРАБОТАЛ! Введена конечная дата: {message.text}")
    if not _is_manager(message.from_user.id):
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

    logger.info(f"📅 Запрос сводки за период {date_from} – {date_to} (branch_id={BRANCH_ID})")
    try:
        lessons = await alfacrm.get_lessons(date_from=date_from, date_to=date_to)
        logger.info(f"📊 Получено {len(lessons)} уроков")
        if not lessons:
            await message.answer(f"📊 Сводка за {date_from} – {date_to}\n\nЗа этот период уроков не было.")
            return
    except Exception as e:
        logger.error(f"❌ Ошибка получения уроков: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка получения уроков: {e}")
        return

    if lessons:
        for i, l in enumerate(lessons[:5]):
            logger.info(f"  Урок {i+1}: id={l.get('id')}, date={l.get('date')}, status={l.get('status')}")

    period_label = f"за {date_from} – {date_to}"
    try:
        summary_text = await get_lesson_summary(lessons, db, alfacrm, period_label)
        logger.info(f"📝 Сводка сформирована, длина {len(summary_text)} символов")
    except Exception as e:
        logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка формирования сводки: {e}")
        return

    try:
        if len(summary_text) > 4096:
            for x in range(0, len(summary_text), 4096):
                await message.answer(summary_text[x:x+4096], parse_mode="HTML")
        else:
            await message.answer(summary_text, parse_mode="HTML")
        logger.info("✅ Сводка отправлена")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка отправки: {e}")