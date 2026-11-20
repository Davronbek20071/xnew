from __future__ import annotations

import asyncio

from loguru import logger

from app.bot.dispatcher import create_bot_and_dispatcher
from app.config import get_config
from app.db.repository import SupabaseRepository
from app.logger import setup_logger
from app.security import SessionCipher
from app.services.notifier import AdminNotifier
from app.userbot.manager import AccountManager


async def main() -> None:
    """VPS / Docker / Lokal 24/7 rejimi: Admin Bot (polling) + Userbot Daemon."""
    config = get_config()
    setup_logger(config.log_level)
    logger.info("X Obunachi Multi-Userbot (24/7 VPS Daemon) ishga tushmoqda...")

    repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
    notifier = AdminNotifier(config.bot_token, config.admin_ids)
    cipher = SessionCipher(config.session_encryption_key)

    bot, dp = create_bot_and_dispatcher(config=config, repo=repo, cipher=cipher)
    manager = AccountManager(config=config, repo=repo, notifier=notifier, cipher=cipher)

    # Agar avval webhook o'rnatilgan bo'lsa, polling ishlashi uchun uni o'chiramiz
    await bot.delete_webhook(drop_pending_updates=True)

    daemon_task = asyncio.create_task(manager.run_forever_daemon(), name="account-manager-daemon")
    try:
        await dp.start_polling(bot)
    finally:
        daemon_task.cancel()
        await bot.session.close()
        await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
