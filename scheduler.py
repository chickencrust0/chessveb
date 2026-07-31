import asyncio
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.keyboards import lesson_action_keyboard
from bot.handlers.common import get_lesson_summary

logger = logging.getLogger(__name__)

STATUS_PLANNED = int(os.getenv('STATUS_PLANNED', '1'))
STATUS_CANCELLED = int(os.getenv('STATUS_CANCELLED', '2'))
STATUS_CONDUCTED = int(os.getenv('STATUS_CONDUCTED', '3'))
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
    
    async def send_daily_schedule(self):
        today = datetime.now().date()
        for role in ["teacher", "parent"]:
            users = self.db.get_all_users_by_role(role)
            for user in users:
                try:
                    if role == "teacher":
                        all_lessons = await self.alfacrm.get_lessons(teacher_id=user["crm_id"])
                    else:
                        all_lessons = await self.alfacrm.get_lessons(customer_id=user["crm_id"])
                    lessons = [l for l in all_lessons if l.get("date") == today.isoformat() and l.get("status") == STATUS_PLANNED]
                    if lessons:
                        lines = [f"📅 <b>Расписание на сегодня</b>:\n"]
                        for lesson in sorted(lessons, key=lambda l: l.get("time_from", "")):
                            time_from = lesson.get("time_from", "")[-8:-3] if lesson.get("time_from") else "??:??"
                            time_to = lesson.get("time_to", "")[-8:-3] if lesson.get("time_to") else "??:??"
                            lines.append(f"🕐 {time_from}–{time_to} | {lesson.get('lesson_type_name', 'Урок')}")
                        await self.bot.send_message(user["telegram_id"], "\n".join(lines), parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['telegram_id']}: {e}")
    
    async def check_upcoming_lessons(self):
        now = datetime.now()
        today = now.date()
        try:
            all_lessons = await self.alfacrm.get_lessons(status=STATUS_PLANNED)
            today_lessons = [l for l in all_lessons if l.get("date") == today.isoformat()]
            for lesson in today_lessons:
                time_from_str = lesson.get("time_from", "")
                if not time_from_str:
                    continue
                try:
                    lesson_time = datetime.fromisoformat(time_from_str)
                    minutes_left = (lesson_time - now).total_seconds() / 60
                    if 55 <= minutes_left <= 65:
                        await self._notify_lesson(lesson, "1 час")
                    elif 12 <= minutes_left <= 18:
                        await self._notify_lesson(lesson, "15 минут")
                except (ValueError, TypeError):
                    continue
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки уроков: {e}")
    
    async def _notify_lesson(self, lesson, when):
        time_str = lesson.get("time_from", "")[-8:-3] if lesson.get("time_from") else "??:??"
        for teacher_id in lesson.get("teacher_ids", []):
            teacher = self.db.get_user_by_crm_id(teacher_id, "teacher")
            if teacher and not self.db.was_reminder_sent(lesson["id"], f"upcoming_{when}", hours=1):
                await self.bot.send_message(
                    teacher["telegram_id"],
                    f"⏰ Урок через <b>{when}</b>! 🕐 {time_str}",
                    parse_mode="HTML",
                    reply_markup=lesson_action_keyboard(lesson["id"])
                )
                self.db.mark_close_reminder_sent(lesson["id"], teacher["telegram_id"])
        for customer_id in lesson.get("customer_ids", []):
            parent = self.db.get_user_by_crm_id(customer_id, "parent")
            if parent:
                await self.bot.send_message(
                    parent["telegram_id"],
                    f"⏰ Занятие через <b>{when}</b>! 🕐 {time_str}",
                    parse_mode="HTML"
                )
    
    # ==================== ЕЖЕДНЕВНАЯ СВОДКА ====================
    async def send_daily_summary(self):
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        try:
            logger.info(f"📅 Запрос ежедневной сводки за {yesterday}")
            lessons = await self.alfacrm.get_lessons(date_from=yesterday, date_to=yesterday)
            logger.info(f"📊 Получено {len(lessons)} уроков")
            if not lessons:
                for manager_id in self.manager_ids:
                    await self.bot.send_message(
                        manager_id,
                        f"📊 Сводка за {yesterday}\n\nЗа этот день уроков не было."
                    )
                return
        except Exception as e:
            logger.error(f"❌ Ошибка получения уроков для сводки: {e}", exc_info=True)
            return
        period_label = f"за {yesterday}"
        summary_text = await get_lesson_summary(lessons, self.db, self.alfacrm, period_label)
        for manager_id in self.manager_ids:
            try:
                await self.bot.send_message(manager_id, summary_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")
    
    # ==================== БАЛАНС ====================
    async def check_low_balance(self):
        try:
            customers = await self.alfacrm.load_all_customers(is_study=1)
            for customer in customers:
                paid_count = int(customer.get("paid_count", 0) or 0)
                paid_lesson_count = int(customer.get("paid_lesson_count", 0) or 0)
                remaining = paid_lesson_count + paid_count
                if remaining <= 2:
                    parent = self.db.get_user_by_crm_id(customer["id"], "parent")
                    if parent:
                        await self.bot.send_message(
                            parent["telegram_id"],
                            f"⚠️ <b>Заканчивается абонемент!</b>\n\n"
                            f"Осталось занятий: <b>{max(0, remaining)}</b>\n"
                            f"💰 Баланс: {customer.get('balance', '0')} руб.\n\n"
                            f"Свяжитесь с менеджером для продления.",
                            parse_mode="HTML"
                        )
                    for manager_id in self.manager_ids:
                        await self.bot.send_message(
                            manager_id,
                            f"⚠️ <b>Заканчивается абонемент</b>\n\n"
                            f"👤 {customer.get('name', '—')}\n"
                            f"🆔 CRM ID: {customer['id']}\n"
                            f"Осталось: {max(0, remaining)} занятий\n"
                            f"💰 Баланс: {customer.get('balance', '0')} руб.",
                            parse_mode="HTML"
                        )
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки баланса: {e}")
    
    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("⏹ Планировщик остановлен")