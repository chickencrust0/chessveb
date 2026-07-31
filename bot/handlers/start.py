import logging
import os
import re

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.keyboards import (
    request_phone_keyboard,
    teacher_menu_keyboard,
    parent_menu_keyboard,
    manager_menu_keyboard,
    confirm_logout_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="start")

MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]

ROLE_LABELS = {
    "teacher": "👨‍🏫 Преподаватель",
    "parent": "👨‍👩‍👧 Родитель",
    "manager": "👑 Менеджер",
}


def get_menu_by_role(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    elif role == "parent":
        return parent_menu_keyboard()
    else:
        return manager_menu_keyboard()


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        role = user["role"]
        menu = get_menu_by_role(role)
        await message.answer(
            f"👋 С возвращением, <b>{user['full_name']}</b>!\n\n"
            f"📊 Роль: {ROLE_LABELS.get(role, role)}\n"
            f"🆔 CRM ID: <code>{user['crm_id']}</code>\n\n"
            f"👇 Выберите раздел:",
            reply_markup=menu,
            parse_mode="HTML",
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для входа поделитесь номером телефона.\n\n"
        "📱 Нажмите кнопку ниже или напишите номер вручную\n"
        "<i>Формат: +7(XXX)XXX-XX-XX или 8XXXXXXXXXX</i>",
        reply_markup=request_phone_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.contact)
async def handle_contact(message: Message, db: Database, alfacrm: AlfaCRMClient, state: FSMContext) -> None:
    await state.clear()
    phone = message.contact.phone_number
    await process_phone_login(message, db, alfacrm, phone)


# Этот обработчик срабатывает ТОЛЬКО для неавторизованных пользователей
# и только если текст похож на номер телефона
@router.message(F.text.regexp(r'^\+?\d[\d\-\(\)\s]{5,}$'))
async def handle_manual_phone(message: Message, db: Database, alfacrm: AlfaCRMClient, state: FSMContext) -> None:
    user = db.get_user(message.from_user.id)
    if user:
        # Если пользователь уже авторизован, просто игнорируем – это позволит другим хендлерам (например, состояниям) обработать сообщение
        logger.debug(f"👤 Пользователь {user['telegram_id']} уже авторизован, пропускаем '{message.text}'")
        return
    await state.clear()
    phone = message.text.strip()
    await process_phone_login(message, db, alfacrm, phone)


async def process_phone_login(message: Message, db: Database, alfacrm: AlfaCRMClient, phone: str):
    telegram_id = message.from_user.id
    logger.info(f"Вход по телефону: {phone}")

    if telegram_id in MANAGER_IDS:
        db.link_user(telegram_id=telegram_id, crm_id=0, role="manager", phone=phone, full_name=message.from_user.full_name or "Менеджер")
        await message.answer("✅ Вы вошли как менеджер.", reply_markup=manager_menu_keyboard())
        return

    try:
        teacher = await alfacrm.find_teacher_by_phone(phone)
        if teacher:
            db.link_user(telegram_id=telegram_id, crm_id=teacher["id"], role="teacher", phone=phone, full_name=alfacrm.extract_user_name(teacher))
            await message.answer(f"✅ Добро пожаловать, {alfacrm.extract_user_name(teacher)}! (Преподаватель)", reply_markup=teacher_menu_keyboard())
            return
    except AlfaCRMError:
        pass

    try:
        customer = await alfacrm.find_customer_by_phone(phone)
        if customer:
            db.link_user(telegram_id=telegram_id, crm_id=customer["id"], role="parent", phone=phone, full_name=alfacrm.extract_user_name(customer))
            await message.answer(f"✅ Добро пожаловать, {alfacrm.extract_user_name(customer)}! (Родитель)", reply_markup=parent_menu_keyboard())
            return
    except AlfaCRMError:
        pass

    await message.answer("❌ Номер не найден в CRM. Попробуйте ещё раз или обратитесь к менеджеру.", reply_markup=request_phone_keyboard())


@router.message(F.text == "🚪 Выйти из профиля")
async def logout_start(message: Message) -> None:
    await message.answer("⚠️ Вы уверены, что хотите выйти?", reply_markup=confirm_logout_keyboard())


@router.callback_query(F.data.startswith("logout:"))
async def logout_process(callback: CallbackQuery, db: Database) -> None:
    action = callback.data.split(":")[1]
    if action == "confirm":
        db.deactivate_user(callback.from_user.id)
        await callback.message.edit_text("👋 Вы вышли.")
        await callback.message.answer("Для входа поделитесь номером телефона:", reply_markup=request_phone_keyboard())
    else:
        await callback.message.edit_text("❌ Отменено.")
    await callback.answer()