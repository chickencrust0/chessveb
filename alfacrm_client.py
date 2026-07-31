import logging
import os
from typing import Optional, Dict, Any, List
import aiohttp

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    1: "📌 запланирован",
    2: "❌ отменён",
    3: "✅ проведён",
}


class AlfaCRMError(Exception):
    pass


class AlfaCRMClient:
    def __init__(self, base_url: str, email: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.email = email
        self.api_key = api_key
        self.branch_id = '1'  # ваш единственный филиал
        self.token = None
        self.session = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def _ensure_token(self):
        if not self.token:
            await self._auth()
    
    async def _auth(self):
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/v2api/auth/login",
                json={"email": self.email, "api_key": self.api_key}
            ) as response:
                data = await response.json()
                if response.status != 200:
                    raise AlfaCRMError(f"Ошибка авторизации: {data}")
                self.token = data.get("token")
                if not self.token:
                    raise AlfaCRMError("Токен не получен")
                logger.info("✅ Токен AlfaCRM получен")
        except aiohttp.ClientError as e:
            raise AlfaCRMError(f"Ошибка сети: {e}")
    
    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        await self._ensure_token()
        session = await self._get_session()
        
        url = f"{self.base_url}{endpoint}"
        headers = {"X-ALFACRM-TOKEN": self.token, "Content-Type": "application/json"}
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        
        logger.info(f"📤 Запрос: {method} {url}")
        if "json" in kwargs:
            logger.info(f"📦 Тело: {kwargs['json']}")
        
        try:
            async with session.request(method, url, headers=headers, **kwargs) as response:
                if response.status == 401:
                    logger.warning("🔄 Токен истёк, обновляем...")
                    await self._auth()
                    headers["X-ALFACRM-TOKEN"] = self.token
                    async with session.request(method, url, headers=headers, **kwargs) as retry:
                        retry.raise_for_status()
                        return await retry.json()
                response.raise_for_status()
                data = await response.json()
                logger.info(f"✅ Ответ получен, статус {response.status}")
                return data
        except aiohttp.ClientError as e:
            logger.error(f"❌ Ошибка сети: {e}")
            raise AlfaCRMError(f"Ошибка сети: {e}")
        except Exception as e:
            logger.error(f"❌ Ошибка запроса: {e}")
            raise AlfaCRMError(f"Ошибка запроса: {e}")
    
    async def _load_all_pages(self, endpoint: str, payload: dict) -> List[Dict]:
        all_items = []
        page = 0
        while True:
            payload["page"] = page
            result = await self._make_request("POST", endpoint, json=payload)
            items = result.get("items", [])
            if not items:
                break
            all_items.extend(items)
            total = result.get("total", 0)
            if len(all_items) >= total:
                break
            page += 1
        return all_items
    
    async def _fetch_lessons_raw(self) -> List[Dict]:
        return await self.get_lessons()
    
    def _normalize_phone(self, phone: str) -> str:
        return ''.join(filter(str.isdigit, phone))
    
    def _phone_matches(self, phone1: str, phone2: str) -> bool:
        clean1 = self._normalize_phone(phone1)
        clean2 = self._normalize_phone(phone2)
        if not clean1 or not clean2:
            return False
        if clean1 == clean2:
            return True
        if len(clean1) >= 10 and len(clean2) >= 10:
            return clean1[-10:] == clean2[-10:]
        return clean1 in clean2 or clean2 in clean1
    
    # ==================== ПОЛЬЗОВАТЕЛИ ====================
    
    async def find_teacher_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/teacher/index", json={"page": 0})
            for teacher in result.get("items", []):
                for t_phone in teacher.get("phone", []):
                    if self._phone_matches(phone, t_phone):
                        return teacher
        except AlfaCRMError:
            pass
        return None
    
    async def find_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/customer/index", json={"page": 0, "is_study": 1})
            for customer in result.get("items", []):
                for c_phone in customer.get("phone", []):
                    if self._phone_matches(phone, c_phone):
                        return customer
        except AlfaCRMError:
            pass
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/customer/index", json={"page": 0})
            for customer in result.get("items", []):
                for c_phone in customer.get("phone", []):
                    if self._phone_matches(phone, c_phone):
                        return customer
        except AlfaCRMError:
            pass
        return None
    
    async def get_teacher_info(self, teacher_id: int) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/teacher/index", json={"page": 0})
            for teacher in result.get("items", []):
                if teacher.get("id") == teacher_id:
                    return teacher
        except AlfaCRMError:
            pass
        return None
    
    async def get_customer_info(self, customer_id: int) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/customer/index", json={"page": 0, "is_study": 1})
            for customer in result.get("items", []):
                if customer.get("id") == customer_id:
                    return customer
        except AlfaCRMError:
            pass
        try:
            result = await self._make_request("POST", f"/v2api/{self.branch_id}/customer/index", json={"page": 0})
            for customer in result.get("items", []):
                if customer.get("id") == customer_id:
                    return customer
        except AlfaCRMError:
            pass
        return None
    
    async def load_all_customers(self, is_study: Optional[int] = None) -> List[Dict]:
        payload = {}
        if is_study is not None:
            payload["is_study"] = is_study
        return await self._load_all_pages(f"/v2api/{self.branch_id}/customer/index", payload)
    
    # ==================== УРОКИ (ИСПРАВЛЕННАЯ ВЕРСИЯ) ====================
    
    async def get_lessons(
        self,
        teacher_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получение уроков с автоматическим перебором всех статусов,
        если status не указан явно (т.к. AlfaCRM по умолчанию отдаёт только status=3).
        """
        # Если статус задан, запрашиваем только его, иначе все три статуса
        statuses_to_fetch = [status] if status is not None else [1, 2, 3]
        
        # Базовые параметры (общие для всех статусов)
        base_payload = {}
        if teacher_id:
            base_payload["teacher_id"] = teacher_id
        if customer_id:
            base_payload["customer_id"] = customer_id
        # ВАЖНО: для lesson/index формат даты — YYYY-MM-DD (как в документации)
        if date_from:
            base_payload["date_from"] = date_from
        if date_to:
            base_payload["date_to"] = date_to
        
        all_lessons: List[Dict[str, Any]] = []
        
        try:
            for st in statuses_to_fetch:
                payload = {**base_payload, "status": st, "page": 0}
                page = 0
                while True:
                    payload["page"] = page
                    result = await self._make_request("POST", f"/v2api/{self.branch_id}/lesson/index", json=payload)
                    items = result.get("items", [])
                    if not items:
                        break
                    all_lessons.extend(items)
                    total = result.get("total", 0)
                    # Проверяем, сколько уроков этого статуса уже собрано
                    fetched_for_status = sum(1 for l in all_lessons if l.get("status") == st)
                    if fetched_for_status >= total:
                        break
                    page += 1
            
            logger.info(f"📊 Получено уроков из API (все статусы): {len(all_lessons)}")
            return all_lessons
        except AlfaCRMError as e:
            logger.error(f"❌ Ошибка получения уроков: {e}")
            raise
    
    async def get_lesson(self, lesson_id: int) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request("GET", f"/v2api/{self.branch_id}/lesson/{lesson_id}")
            return result.get("data") or result
        except AlfaCRMError:
            pass
        try:
            lessons = await self.get_lessons()
            for lesson in lessons:
                if lesson.get("id") == lesson_id:
                    return lesson
        except AlfaCRMError:
            pass
        return None
    
    async def update_lesson(self, lesson_id: int, data: Dict[str, Any]) -> bool:
        try:
            await self._make_request(
                "POST",
                f"/v2api/{self.branch_id}/lesson/update",
                params={"id": lesson_id},
                json=data
            )
            logger.info(f"✅ Урок {lesson_id} обновлён: {list(data.keys())}")
            return True
        except AlfaCRMError as e:
            logger.error(f"❌ Ошибка обновления урока {lesson_id}: {e}")
            raise
    
    async def mark_lesson_conducted(self, lesson_id: int) -> bool:
        return await self.update_lesson(lesson_id, {"status": 3})
    
    async def set_homework(self, lesson_id: int, homework_text: str) -> bool:
        return await self.update_lesson(lesson_id, {"homework": homework_text})
    
    def extract_user_name(self, user: Dict) -> str:
        return user.get("name", "") or user.get("legal_name", "") or "Без имени"
    
    def extract_user_phone(self, user: Dict) -> str:
        phones = user.get("phone", [])
        return phones[0] if phones else "Нет телефона"
    
    def get_lesson_status_label(self, status: int) -> str:
        return STATUS_LABELS.get(status, f"статус {status}")
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()