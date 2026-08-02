import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.keyboards import lesson_action_keyboard
from bot.formatting import (
    STATUS_PLANNED,
    build_schedule,
    fmt_date_long,
    format_reminder,
    lesson_date_iso,
)
from bot.handlers.common import get_lesson_summary, load_customer_map, send_blocks

logger = logging.getLogger(__name__)

MANAGER_IDS = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]


class ReminderScheduler:
    def __init__(self, db: Database, alfacrm: AlfaCRMClient, bot):
        self.db = db
        self.alfacrm = alfacrm
        self.bot = bot
        self.scheduler = AsyncIOScheduler()
        self.manager_ids = MANAGER_IDS

    def start(self):
        self.scheduler.add_job(self.send_daily_schedule, CronTrigger(hour=8, minute=0), id="daily")
        self.scheduler.add_job(self.check_upcoming_lessons, IntervalTrigger(minutes=5), id="upcoming")
        self.scheduler.add_job(self.check_low_balance, CronTrigger(hour=10, minute=0), id="balance")
        self.scheduler.add_job(self.send_daily_summary, CronTrigger(hour=0, minute=1), id="daily_summary")
        self.scheduler.start()
        logger.info("✅ Планировщик запущен")

    # ==================== УТРЕННЯЯ РАССЫЛКА ====================

    async def send_daily_schedule(self):
        today = datetime.now().date()
        today_iso = today.isoformat()

        try:
            customers = await load_customer_map(self.alfacrm)
        except Exception as e:
            logger.error(f"Не удалось загрузить карту учеников: {e}")
            customers = {}

        for role in ("teacher", "parent"):
            for user in self.db.get_all_users_by_role(role):
                try:
                    if role == "teacher":
                        all_lessons = await self.alfacrm.get_lessons(
                            teacher_id=user["crm_id"], date_from=today_iso, date_to=today_iso
                        )
                    else:
                        all_lessons = await self.alfacrm.get_lessons(
                            customer_id=user["crm_id"], date_from=today_iso, date_to=today_iso
                        )

                    lessons = [
                        l for l in all_lessons
                        if lesson_date_iso(l) == today_iso and l.get("status") == STATUS_PLANNED
                    ]
                    if not lessons:
                        continue

                    blocks = build_schedule(
                        lessons,
                        role=role,
                        title=f"Расписание на сегодня, {fmt_date_long(today)}",
                        customers=customers,
                    )
                    await send_blocks(self.bot, user["telegram_id"], blocks)
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['telegram_id']}: {e}")

    # ==================== НАПОМИНАНИЯ ====================

    async def check_upcoming_lessons(self):
        now = datetime.now()
        today_iso = now.date().isoformat()

        try:
            all_lessons = await self.alfacrm.get_lessons(
                status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso
            )
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки уроков: {e}")
            return

        for lesson in all_lessons:
            if lesson_date_iso(lesson) != today_iso:
                continue
            time_from_str = lesson.get("time_from")
            if not time_from_str:
                continue
            try:
                lesson_time = datetime.fromisoformat(str(time_from_str).replace("T", " "))
            except (ValueError, TypeError):
                continue

            minutes_left = (lesson_time - now).total_seconds() / 60
            if 55 <= minutes_left <= 65:
                await self._notify_lesson(lesson, "1 час")
            elif 12 <= minutes_left <= 18:
                await self._notify_lesson(lesson, "15 минут")

    async def _notify_lesson(self, lesson, when: str):
        try:
            customers = await load_customer_map(self.alfacrm)
        except Exception:
            customers = {}

        for teacher_id in lesson.get("teacher_ids", []):
            teacher = self.db.get_user_by_crm_id(teacher_id, "teacher")
            if not teacher:
                continue
            if self.db.was_reminder_sent(lesson["id"], f"upcoming_{when}", hours=1):
                continue
            try:
                await self.bot.send_message(
                    teacher["telegram_id"],
                    format_reminder(lesson, when=when, role="teacher", customers=customers),
                    parse_mode="HTML",
                    reply_markup=lesson_action_keyboard(lesson["id"]),
                )
                self.db.mark_close_reminder_sent(lesson["id"], teacher["telegram_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить преподавателю {teacher['telegram_id']}: {e}")

        for customer_id in lesson.get("customer_ids", []):
            parent = self.db.get_user_by_crm_id(customer_id, "parent")
            if not parent:
                continue
            try:
                await self.bot.send_message(
                    parent["telegram_id"],
                    format_reminder(lesson, when=when, role="parent"),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Не удалось напомнить родителю {parent['telegram_id']}: {e}")

    # ==================== ЕЖЕДНЕВНАЯ СВОДКА ====================

    async def send_daily_summary(self):
        yesterday = (datetime.now().date() - timedelta(days=1))
        yesterday_iso = yesterday.isoformat()
        period_label = f"за {fmt_date_long(yesterday)}"

        try:
            logger.info(f"📅 Запрос ежедневной сводки за {yesterday_iso}")
            all_lessons = await self.alfacrm.get_lessons(
                date_from=yesterday_iso, date_to=yesterday_iso
            )
            lessons = [l for l in all_lessons if lesson_date_iso(l) == yesterday_iso]
            logger.info(f"📊 Получено {len(lessons)} уроков")
        except Exception as e:
            logger.error(f"❌ Ошибка получения уроков для сводки: {e}", exc_info=True)
            return

        try:
            blocks = await get_lesson_summary(lessons, self.db, self.alfacrm, period_label)
        except Exception as e:
            logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
            return

        for manager_id in self.manager_ids:
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")

    # ==================== БАЛАНС ====================

    async def check_low_balance(self):
        today = datetime.now().date()
        try:
            customers = await self.alfacrm.load_all_customers(is_study=1)
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки баланса: {e}")
            return

        for customer in customers:
            paid_count = int(customer.get("paid_count", 0) or 0)
            paid_lesson_count = int(customer.get("paid_lesson_count", 0) or 0)
            remaining = max(0, paid_lesson_count + paid_count)
            if remaining > 2:
                continue

            next_lesson = customer.get("next_lesson_date") or "—"

            parent = self.db.get_user_by_crm_id(customer["id"], "parent")
            if parent:
                try:
                    await self.bot.send_message(
                        parent["telegram_id"],
                        f"⚠️ <b>Заканчивается абонемент</b>\n\n"
                        f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}\n"
                        f"🎟 <b>Осталось занятий:</b> {remaining}\n"
                        f"💰 <b>Баланс:</b> {customer.get('balance', '0')} руб.\n"
                        f"➡️ <b>Следующее занятие:</b> {next_lesson}\n\n"
                        f"Свяжитесь с менеджером для продления.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить родителя {parent['telegram_id']}: {e}")

            for manager_id in self.manager_ids:
                try:
                    await self.bot.send_message(
                        manager_id,
                        f"⚠️ <b>Заканчивается абонемент</b>\n\n"
                        f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}\n"
                        f"👤 <b>Ученик:</b> {customer.get('name', '—')}\n"
                        f"🆔 <b>CRM ID:</b> {customer['id']}\n"
                        f"🎟 <b>Осталось занятий:</b> {remaining}\n"
                        f"💰 <b>Баланс:</b> {customer.get('balance', '0')} руб.\n"
                        f"➡️ <b>Следующее занятие:</b> {next_lesson}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить менеджера {manager_id}: {e}")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("⏹ Планировщик остановлен")
