from __future__ import annotations

from typing import Any, Dict, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from app.db.repository import SupabaseRepository


class SupabaseFSMStorage(BaseStorage):
    """Vercel serverless muhiti uchun Supabase'ga asoslangan Aiogram 3 FSM Storage.

    Har bir webhook chaqiruvi mustaqil jarayonda ishlagani sababli, FSM holati va
    vaqtinchalik ma'lumotlar (masalan, Telethon login kodi) `fsm_states` jadvalida saqlanadi.
    """

    def __init__(self, repo: SupabaseRepository) -> None:
        self.repo = repo

    @staticmethod
    def _build_key(key: StorageKey) -> str:
        return f"fsm:{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        s_key = self._build_key(key)
        current = await self.repo.fsm_get(s_key)
        state_str = state.state if isinstance(state, State) else state
        if state_str is None and not current.get("data"):
            await self.repo.fsm_delete(s_key)
        else:
            await self.repo.fsm_upsert(s_key, state=state_str, data=current.get("data") or {})

    async def get_state(self, key: StorageKey) -> Optional[str]:
        current = await self.repo.fsm_get(self._build_key(key))
        return current.get("state")

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        s_key = self._build_key(key)
        current = await self.repo.fsm_get(s_key)
        if not data and current.get("state") is None:
            await self.repo.fsm_delete(s_key)
        else:
            await self.repo.fsm_upsert(s_key, state=current.get("state"), data=data or {})

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        current = await self.repo.fsm_get(self._build_key(key))
        return dict(current.get("data") or {})

    async def close(self) -> None:
        return None
