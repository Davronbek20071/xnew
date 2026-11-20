from __future__ import annotations

from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.db.models import ProxyModel
from app.services.proxy_checker import build_telethon_proxy


def create_telegram_client(
    *,
    session_string: str,
    api_id: int,
    api_hash: str,
    proxy: Optional[ProxyModel] = None,
) -> TelegramClient:
    """Telethon TelegramClient obyektini StringSession va proksi bilan yaratadi."""
    proxy_cfg = build_telethon_proxy(proxy)
    return TelegramClient(
        StringSession(session_string or ""),
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy_cfg,
        device_model="Samsung Galaxy S23",
        system_version="Android 14",
        app_version="10.14.5",
        lang_code="uz",
        system_lang_code="uz-UZ",
        connection_retries=3,
        retry_delay=2,
        timeout=15,
    )
