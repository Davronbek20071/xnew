from __future__ import annotations

import asyncio

from loguru import logger

from app.config import get_config
from app.db.repository import SupabaseRepository
from app.logger import setup_logger
from app.security import SessionCipher
from app.services.notifier import AdminNotifier
from app.userbot.manager import AccountManager


async def main() -> None:
    config = get_config()
    setup_logger(config.log_level)
    logger.info("Auto-Leave Worker (16 kunlik nazorat) ishga tushdi.")

    repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
    notifier = AdminNotifier(config.bot_token, config.admin_ids)
    cipher = SessionCipher(config.session_encryption_key)
    manager = AccountManager(config=config, repo=repo, notifier=notifier, cipher=cipher)

    try:
        await manager.run_auto_leave_all()
    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
