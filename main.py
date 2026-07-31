import asyncio
import logging
import sys
import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

bot_token = os.getenv('BOT_TOKEN', '').strip()
alfacrm_email = os.getenv('ALFACRM_EMAIL', '').strip()
alfacrm_api_key = os.getenv('ALFACRM_API_KEY', '').strip()
PROXY_URL = os.getenv('PROXY_URL', '').strip()

if not bot_token or bot_token == 'your_telegram_bot_token_here':
    print('❌ Токен бота не указан!')
    sys.exit(1)

print(f"BOT_TOKEN: {'***' if bot_token else 'НЕ НАЙДЕН!'}")
print(f"PROXY_URL: {PROXY_URL if PROXY_URL else 'НЕ НАЙДЕН!'}")
print()

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

from database import Database
from alfacrm_client import AlfaCRMClient
from scheduler import ReminderScheduler
from bot.handlers import start, teacher, parent, manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


class Config:
    def __init__(self):
        self.bot_token = bot_token
        self.alfacrm_url = os.getenv('ALFACRM_URL', 'https://onlaynshkolashahmat.s20.online')
        self.alfacrm_email = alfacrm_email
        self.alfacrm_api_key = alfacrm_api_key
        self.manager_telegram_ids = [int(x.strip()) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip().isdigit()]
        self.STATUS_PLANNED = int(os.getenv('STATUS_PLANNED', '1'))
        self.STATUS_CANCELLED = int(os.getenv('STATUS_CANCELLED', '2'))
        self.STATUS_CONDUCTED = int(os.getenv('STATUS_CONDUCTED', '3'))
        self.STATUS_LABELS = {1: "📌 запланирован", 2: "❌ отменён", 3: "✅ проведён"}
    
    def get_status_label(self, status):
        return self.STATUS_LABELS.get(status, f"статус {status}")


config = Config()


async def main():
    logger.info("Запуск бота...")
    
    db = Database(str(Path(__file__).parent / "bot.db"))
    logger.info("✅ База данных")
    
    alfacrm = AlfaCRMClient(
        base_url=config.alfacrm_url,
        email=config.alfacrm_email,
        api_key=config.alfacrm_api_key
    )
    logger.info("✅ Клиент AlfaCRM")
    
    session = None
    if PROXY_URL:
        try:
            session = AiohttpSession(proxy=PROXY_URL)
            logger.info(f"✅ Прокси: {PROXY_URL}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка прокси: {e}")
    
    bot = Bot(token=config.bot_token, session=session, timeout=60) if session else Bot(token=config.bot_token)
    
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    
    # ✅ ИЗМЕНЁН ПОРЯДОК: manager идёт ПЕРВЫМ
    dp.include_router(manager.router)
    dp.include_router(teacher.router)
    dp.include_router(parent.router)
    dp.include_router(start.router)      # start последним
    
    scheduler = None
    
    try:
        me = await bot.get_me()
        print(f"\n✅ Бот @{me.username} запущен!\n")
        
        scheduler = ReminderScheduler(db, alfacrm, bot)
        scheduler.start()
        
        print("📡 Бот ожидает сообщения...\n")
        
        await dp.start_polling(bot, db=db, alfacrm=alfacrm, config=config)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        print(f"\n❌ {e}")
    finally:
        if scheduler:
            scheduler.stop()
        await alfacrm.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")