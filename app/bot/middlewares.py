from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AdminOnlyMiddleware(BaseMiddleware):
    """Faqat .env dagi ADMIN_IDS ro'yxatidagi foydalanuvchilarga ruxsat beradi."""

    def __init__(self, admin_ids: Iterable[int]) -> None:
        self.admin_ids = set(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id not in self.admin_ids:
            if isinstance(event, Message):
                await event.answer("⛔ Sizda ushbu boshqaruv paneliga kirish huquqi yo‘q.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Ruxsat yo‘q!", show_alert=True)
            return None
        return await handler(event, data)
