from __future__ import annotations

from typing import Iterable

import httpx
from loguru import logger


class AdminNotifier:
    """Muhim xatoliklar va hisobotlarni barcha ADMIN_IDS ga Telegram orqali yuboradi."""

    def __init__(self, bot_token: str, admin_ids: Iterable[int]) -> None:
        self.bot_token = bot_token
        self.admin_ids = list(admin_ids)
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async def notify(self, text: str) -> None:
        if not self.bot_token or not self.admin_ids:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            for admin_id in self.admin_ids:
                try:
                    resp = await client.post(
                        self.api_url,
                        json={
                            "chat_id": admin_id,
                            "text": text[:4000],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                    if resp.status_code >= 400:
                        logger.warning(
                            "Adminga ({}) xabar yuborilmadi: {} {}",
                            admin_id,
                            resp.status_code,
                            resp.text[:200],
                        )
                except Exception as exc:
                    logger.warning("Adminga ({}) xabar yuborishda xato: {}", admin_id, exc)
