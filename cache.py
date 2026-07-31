# cache.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from alfacrm_client import AlfaCRMClient, AlfaCRMError

logger = logging.getLogger(__name__)


class LessonCache:
    """Кеш уроков с автоматическим обновлением каждые 2 минуты."""
    
    def __init__(self, alfacrm: AlfaCRMClient):
        self.alfacrm = alfacrm
        self._lessons_by_id: Dict[int, Dict[str, Any]] = {}
        self._lessons_by_teacher: Dict[int, List[Dict[str, Any]]] = {}
        self._lessons_by_customer: Dict[int, List[Dict[str, Any]]] = {}
        self._all_lessons: List[Dict[str, Any]] = []
        self._last_update = None
        self.scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._initialized = False
        
    def start(self):
        """Запускает периодическое обновление кеша."""
        # Первое обновление сразу
        asyncio.create_task(self.refresh())
        # Затем каждые 2 минуты
        self.scheduler.add_job(
            self.refresh,
            IntervalTrigger(minutes=2),
            id="lesson_cache_refresh",
            name="Обновление кеша уроков"
        )
        self.scheduler.start()
        logger.info("Кеш уроков запущен (обновление каждые 2 минуты)")
    
    async def refresh(self):
        """Обновляет кеш, запрашивая все уроки (без фильтрации по датам)."""
        async with self._lock:
            try:
                logger.info("🔄 Запрос всех уроков из API (без фильтрации по датам)")
                
                # Запрашиваем все уроки без фильтрации по датам
                # Фильтрацию по датам будем делать на стороне кеша
                lessons = await self.alfacrm._fetch_lessons_raw()
                
                logger.info(f"📊 Получено {len(lessons)} уроков из API")
                
                # Логируем первые 3 урока для диагностики
                if lessons:
                    for i, lesson in enumerate(lessons[:3]):
                        logger.info(f"  Урок {i+1}: id={lesson.get('id')}, date={lesson.get('date')}, "
                                   f"status={lesson.get('status')}, customer_ids={lesson.get('customer_ids', [])}")
                
                # Обновляем индексы
                self._all_lessons = lessons
                self._lessons_by_id = {l['id']: l for l in lessons}
                
                # Индексы по преподавателям
                by_teacher = {}
                for l in lessons:
                    for t_id in l.get('teacher_ids', []):
                        by_teacher.setdefault(t_id, []).append(l)
                self._lessons_by_teacher = by_teacher
                
                # Индексы по ученикам
                by_customer = {}
                for l in lessons:
                    for c_id in l.get('customer_ids', []):
                        by_customer.setdefault(c_id, []).append(l)
                self._lessons_by_customer = by_customer
                
                self._last_update = datetime.now()
                self._initialized = True
                logger.info(f"✅ Кеш уроков обновлён: {len(lessons)} уроков")
                logger.info(f"  👨‍🏫 Преподавателей в кеше: {len(by_teacher)}")
                logger.info(f"  👨‍🎓 Учеников в кеше: {len(by_customer)}")
                
                if len(lessons) == 0:
                    logger.warning("⚠️ Кеш содержит 0 уроков. Проверьте branch_id, авторизацию и наличие уроков в CRM.")
                    
            except Exception as e:
                logger.error(f"❌ Ошибка обновления кеша уроков: {e}", exc_info=True)
    
    def get_lessons(
        self,
        teacher_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        lesson_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить уроки из кеша с фильтрацией.
        """
        if not self._initialized:
            logger.warning("⚠️ Кеш ещё не инициализирован, возвращаем пустой список")
            return []
        
        if lesson_id is not None:
            lesson = self._lessons_by_id.get(lesson_id)
            return [lesson] if lesson else []
        
        # Начинаем с полного списка
        if teacher_id is not None:
            lessons = self._lessons_by_teacher.get(teacher_id, [])
            logger.debug(f"Поиск по преподавателю {teacher_id}: найдено {len(lessons)} уроков")
        elif customer_id is not None:
            lessons = self._lessons_by_customer.get(customer_id, [])
            logger.debug(f"Поиск по ученику {customer_id}: найдено {len(lessons)} уроков")
        else:
            lessons = self._all_lessons
            logger.debug(f"Поиск по всем урокам: {len(lessons)}")
        
        # Фильтрация по статусу
        if status is not None:
            lessons = [l for l in lessons if l.get('status') == status]
            logger.debug(f"После фильтра по статусу {status}: {len(lessons)} уроков")
        
        # Фильтрация по датам (на стороне кеша)
        if date_from or date_to:
            def in_range(date_str: str) -> bool:
                if not date_str:
                    return False
                # Если дата в формате DD.MM.YYYY, конвертируем в YYYY-MM-DD
                if '.' in date_str:
                    try:
                        parts = date_str.split('.')
                        date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    except:
                        pass
                if date_from and date_str < date_from:
                    return False
                if date_to and date_str > date_to:
                    return False
                return True
            lessons = [l for l in lessons if in_range(l.get('date', ''))]
            logger.debug(f"После фильтра по датам ({date_from} - {date_to}): {len(lessons)} уроков")
        
        return lessons
    
    def get_lesson(self, lesson_id: int) -> Optional[Dict[str, Any]]:
        return self._lessons_by_id.get(lesson_id)
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику кеша для диагностики."""
        return {
            "total_lessons": len(self._all_lessons),
            "teachers_count": len(self._lessons_by_teacher),
            "customers_count": len(self._lessons_by_customer),
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "initialized": self._initialized
        }
    
    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("Кеш уроков остановлен")