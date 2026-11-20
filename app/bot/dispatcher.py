from __future__ import annotations

from typing import Tuple

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router as admin_router
from app.bot.middlewares import AdminOnlyMiddleware
from app.bot.storage import SupabaseFSMStorage
from app.config import AppConfig
from app.db.repository import SupabaseRepository
from app.security import SessionCipher


def create_bot_and_dispatcher(
    *,
    config: AppConfig,
    repo: SupabaseRepository,
    cipher: SessionCipher,
) -> Tuple[Bot, Dispatcher]:
    """Aiogram 3 Bot va Dispatcher obyektlarini yig'adi (Vercel va VPS uchun bir xil)."""
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = SupabaseFSMStorage(repo=repo)
    dp = Dispatcher(storage=storage)

    # Dependency injection (handler parametrlari: config, repo, cipher)
    dp["config"] = config
    dp["repo"] = repo
    dp["cipher"] = cipher

    # Admin whitelist himoyasi
    admin_mw = AdminOnlyMiddleware(config.admin_ids)
    dp.message.middleware(admin_mw)
    dp.callback_query.middleware(admin_mw)

    # Vercel warm-containerda qayta chaqirilganda "Router is already attached"
    # xatosi chiqmasligi uchun avvalgi bog'lanishni uzamiz.
    if getattr(admin_router, "_parent_router", None) is not None:
        admin_router._parent_router = None

    dp.include_router(admin_router)
    return bot, dp
