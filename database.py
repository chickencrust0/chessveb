import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List, Sequence
import logging

logger = logging.getLogger(__name__)

# SQLite ограничивает число параметров в запросе (обычно 999).
_SQL_VAR_CHUNK = 500


class Database:
    def __init__(self, db_path: str = "bot.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    crm_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher', 'parent', 'manager')),
                    phone TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS homework_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    file_type TEXT DEFAULT 'document',
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transfer_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_telegram_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    comment TEXT,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    FOREIGN KEY (teacher_telegram_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS reminder_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    target_telegram_id INTEGER,
                    status TEXT DEFAULT 'sent'
                );

                CREATE TABLE IF NOT EXISTS escalation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    escalation_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    admin_telegram_id INTEGER,
                    message_text TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_homework_lesson ON homework_files(lesson_id);
                CREATE INDEX IF NOT EXISTS idx_transfer_status ON transfer_requests(status);
                CREATE INDEX IF NOT EXISTS idx_reminder_lesson ON reminder_log(lesson_id);
            """)

    def deactivate_user(self, telegram_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))

    def link_user(self, telegram_id: int, crm_id: int, role: str, phone: str, full_name: str):
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET crm_id=?, role=?, phone=?, full_name=?, is_active=1 WHERE telegram_id=?",
                    (crm_id, role, phone, full_name, telegram_id)
                )
            else:
                conn.execute(
                    "INSERT INTO users (telegram_id, crm_id, role, phone, full_name) VALUES (?,?,?,?,?)",
                    (telegram_id, crm_id, role, phone, full_name)
                )

    def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ? AND is_active = 1", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_crm_id(self, crm_id: int, role: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE crm_id = ? AND role = ? AND is_active = 1", (crm_id, role)
            ).fetchone()
            return dict(row) if row else None

    def get_all_users_by_role(self, role: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = ? AND is_active = 1", (role,)
            ).fetchall()
            return [dict(row) for row in rows]

    def add_homework_file(self, lesson_id: int, file_id: str, file_name: str, file_type: str = "document"):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO homework_files (lesson_id, file_id, file_name, file_type) VALUES (?,?,?,?)",
                (lesson_id, file_id, file_name, file_type)
            )

    def get_homework_files(self, lesson_id: int) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM homework_files WHERE lesson_id = ? ORDER BY uploaded_at DESC", (lesson_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_homework_file_counts(
        self, lesson_ids: Optional[Sequence[int]] = None
    ) -> Dict[int, int]:
        """
        {lesson_id: количество файлов ДЗ} одним запросом.

        Нужен для сводок: перебирать get_homework_files() по каждому уроку —
        это сотни запросов на месячный период.
        Без аргумента возвращает счётчики по всем урокам сразу.
        """
        counts: Dict[int, int] = {}
        with self._get_conn() as conn:
            if lesson_ids is None:
                rows = conn.execute(
                    "SELECT lesson_id, COUNT(*) AS cnt FROM homework_files GROUP BY lesson_id"
                ).fetchall()
                return {row["lesson_id"]: row["cnt"] for row in rows}

            ids = [int(i) for i in lesson_ids if i is not None]
            for start in range(0, len(ids), _SQL_VAR_CHUNK):
                chunk = ids[start:start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT lesson_id, COUNT(*) AS cnt FROM homework_files "
                    f"WHERE lesson_id IN ({placeholders}) GROUP BY lesson_id",
                    tuple(chunk)
                ).fetchall()
                for row in rows:
                    counts[row["lesson_id"]] = row["cnt"]
        return counts

    def create_transfer_request(self, teacher_telegram_id: int, lesson_id: int, comment: str) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO transfer_requests (teacher_telegram_id, lesson_id, comment) VALUES (?,?,?)",
                (teacher_telegram_id, lesson_id, comment)
            )
            return cursor.lastrowid

    def get_transfer_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def resolve_transfer_request(self, request_id: int, status: str, resolved_by: int):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE transfer_requests SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=? WHERE id=?",
                (status, resolved_by, request_id)
            )

    def get_pending_transfer_requests(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT tr.*, u.full_name as teacher_name
                   FROM transfer_requests tr
                   JOIN users u ON tr.teacher_telegram_id = u.telegram_id
                   WHERE tr.status = 'pending'
                   ORDER BY tr.created_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_close_reminder_sent(self, lesson_id: int, target_telegram_id: Optional[int] = None):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO reminder_log (lesson_id, reminder_type, target_telegram_id) VALUES (?, 'close_lesson', ?)",
                (lesson_id, target_telegram_id)
            )

    def mark_escalation_sent(self, lesson_id: int, escalation_type: str = "unclosed_lesson"):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO escalation_log (lesson_id, escalation_type) VALUES (?,?)",
                (lesson_id, escalation_type)
            )

    def was_reminder_sent(self, lesson_id: int, reminder_type: str, hours: int = 24) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as count FROM reminder_log
                   WHERE lesson_id=? AND reminder_type=? AND sent_at > datetime('now', ? || ' hours')""",
                (lesson_id, reminder_type, f'-{hours}')
            ).fetchone()
            return row['count'] > 0

    def was_escalation_sent(self, lesson_id: int, escalation_type: str, hours: int = 24) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as count FROM escalation_log
                   WHERE lesson_id=? AND escalation_type=? AND sent_at > datetime('now', ? || ' hours')""",
                (lesson_id, escalation_type, f'-{hours}')
            ).fetchone()
            return row['count'] > 0
