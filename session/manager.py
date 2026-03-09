from typing import Dict, Any, Optional
from telegram_bot.session.store import session_store


class SessionManager:
    VALID_REPLY_MODES = {"text", "voice"}
    DEFAULT_REPLY_MODE = "text"

    def __init__(self):
        self.store = session_store

    @classmethod
    def normalize_reply_mode(cls, mode: Optional[str]) -> str:
        normalized = str(mode or cls.DEFAULT_REPLY_MODE).strip().lower()
        if normalized not in cls.VALID_REPLY_MODES:
            return cls.DEFAULT_REPLY_MODE
        return normalized

    async def _ensure_reply_mode(
        self, user_id: int, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        current_mode = session.get("reply_mode")
        normalized_mode = self.normalize_reply_mode(current_mode)
        if current_mode != normalized_mode:
            session["reply_mode"] = normalized_mode
            await self.store.set(user_id, session)
        return session

    async def get_session(self, user_id: int) -> Dict[str, Any]:
        session = await self.store.get(user_id) or {}
        return await self._ensure_reply_mode(user_id, session)

    async def update_session(self, user_id: int, data: Dict[str, Any]) -> None:
        payload = dict(data)
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.update(user_id, payload)

    async def get_reply_mode(self, user_id: int) -> str:
        session = await self.get_session(user_id)
        return self.normalize_reply_mode(session.get("reply_mode"))

    async def set_reply_mode(self, user_id: int, mode: str) -> None:
        await self.update_session(
            user_id, {"reply_mode": self.normalize_reply_mode(mode)}
        )

    async def clear_session(self, user_id: int) -> None:
        await self.store.delete(user_id)

    async def set_pending_question(
        self, user_id: int, question_id: str, question_data: Dict[str, Any]
    ) -> None:
        await self.update_session(
            user_id, {"pending_question": {"id": question_id, **question_data}}
        )

    async def get_pending_question(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = await self.get_session(user_id)
        return session.get("pending_question")

    async def clear_pending_question(self, user_id: int) -> None:
        session = await self.get_session(user_id)
        if "pending_question" in session:
            del session["pending_question"]
            await self.update_session(user_id, session)


session_manager = SessionManager()
