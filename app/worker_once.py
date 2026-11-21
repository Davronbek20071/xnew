from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from app.config import get_config
from app.db.repository import SupabaseRepository
from app.logger import setup_logger
from app.security import SessionCipher
from app.services.notifier import AdminNotifier
from app.userbot.finance import FinanceAutomation
from app.userbot.manager import AccountManager


async def main(force: bool = False) -> None:
    config = get_config()
    setup_logger(config.log_level)
    logger.info("Userbot Worker (bir siklli rejim) ishga tushdi. force={}", force)

    repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
    notifier = AdminNotifier(config.bot_token, config.admin_ids)
    cipher = SessionCipher(config.session_encryption_key)
    manager = AccountManager(config=config, repo=repo, notifier=notifier, cipher=cipher)

    try:
        await manager.run_once_all(force=force)

        # Agar @imkon7isbot da yakunlanmagan buyurtma sababli navbatda turgan buyurtma bo'lsa,
        # har siklda spamsiz qayta tekshirib, avvalgi buyurtma tugashi bilan darhol buyurtma beradi!
        finance = FinanceAutomation(
            config=config, repo=repo, notifier=notifier, cipher=cipher
        )
        if await finance.has_pending_order():
            logger.info("Navbatda turgan avto-buyurtma aniqlandi, qayta tekshirilmoqda...")
            await finance.run_auto_order(is_retry=True)
    finally:
        await repo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="X Obunachi Userbot 1-cycle runner")
    parser.add_argument(
        "--force",
        action="store_true",
        help="next_run_at vaqtini kutmasdan barcha faol akkauntlarni darhol ishlatish",
    )
    args = parser.parse_args()
    asyncio.run(main(force=args.force))
