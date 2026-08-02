import logging
import os
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import Database
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from bot.keyboards import transfer_decision_keyboard
from bot.states import BroadcastStates, ManagerSummaryStates
from bot.formatting import fmt_date_long, lesson_date_iso
from bot.handlers.common import answer_blocks, get_lesson_summary

logger = logging.getLogger(__name__)
router = Router(name="manager")

MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]
BRANCH_ID = os.getenv('BRANCH_ID', '1')


def _is_manager(telegram_id: int) -> bool:
    return telegram_id in MANAGER_IDS


def _parse_date(text: str):
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ, возвращает date или None."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


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
        except Exception as e:
            logger.warning(f"Рассылка не дошла до {user['telegram_id']}: {e}")

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
        created = req.get("created_at") or "—"
        await message.answer(
            f"🔁 <b>Заявка №{req['id']}</b>\n\n"
            f"📅 <b>Создана:</b> {created}\n"
            f"👤 <b>От кого:</b> {req.get('teacher_name', '—')}\n"
            f"🆔 <b>Урок ID:</b> {req.get('lesson_id', '—')}\n"
            f"💬 <b>Комментарий:</b> {req.get('comment', '—')}",
            parse_mode="HTML",
            reply_markup=transfer_decision_keyboard(req['id']),
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
            await callback.bot.send_message(
                request["teacher_telegram_id"], f"✅ Заявка №{request_id} одобрена!"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить автора заявки {request_id}: {e}")
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
            await callback.bot.send_message(
                request["teacher_telegram_id"], f"❌ Заявка №{request_id} отклонена."
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить автора заявки {request_id}: {e}")
    await callback.message.edit_text(f"❌ Заявка №{request_id} отклонена.")
    await callback.answer()


# ==================== СВОДКА ЗА ПЕРИОД ====================

@router.message(F.text == "📊 Сводка за период")
async def summary_period_start(message: Message, state: FSMContext) -> None:
    if not _is_manager(message.from_user.id):
        return
    await state.set_state(ManagerSummaryStates.waiting_for_date_from)
    today = datetime.now().date()
    await message.answer(
        "📅 Введите <b>начальную</b> дату.\n"
        f"Формат: <code>{today.isoformat()}</code> или <code>{today.strftime('%d.%m.%Y')}</code>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_from, F.text)
async def summary_date_from(message: Message, state: FSMContext) -> None:
    parsed = _parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
            parse_mode="HTML",
        )
        return

    await state.update_data(date_from=parsed.isoformat())
    await state.set_state(ManagerSummaryStates.waiting_for_date_to)
    await message.answer(
        "📅 Теперь <b>конечную</b> дату.\n"
        "<i>Отправьте «-», чтобы взять тот же день.</i>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_to, F.text)
async def summary_date_to(
    message: Message, state: FSMContext, db: Database, alfacrm: AlfaCRMClient
) -> None:
    if not _is_manager(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    date_from = data["date_from"]

    if message.text.strip() in ("-", "—"):
        date_to = date_from
    else:
        parsed = _parse_date(message.text)
        if not parsed:
            await message.answer(
                "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
                parse_mode="HTML",
            )
            return
        date_to = parsed.isoformat()

    await state.clear()

    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await message.answer("🔍 Собираю сводку…")
    logger.info(f"📅 Сводка за {date_from} – {date_to} (branch_id={BRANCH_ID})")

    try:
        all_lessons = await alfacrm.get_lessons(date_from=date_from, date_to=date_to)
        lessons = [l for l in all_lessons if date_from <= lesson_date_iso(l) <= date_to]
        logger.info(f"📊 Получено {len(all_lessons)}, после фильтра по датам {len(lessons)}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения уроков: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка получения уроков: {e}")
        return

    if date_from == date_to:
        period_label = f"за {fmt_date_long(datetime.strptime(date_from, '%Y-%m-%d').date())}"
    else:
        period_label = f"за период {date_from} – {date_to}"

    try:
        blocks = await get_lesson_summary(lessons, db, alfacrm, period_label)
    except Exception as e:
        logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка формирования сводки: {e}")
        return

    try:
        await answer_blocks(message, blocks)
        logger.info(f"✅ Сводка отправлена ({len(blocks)} сообщений)")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка отправки: {e}")
