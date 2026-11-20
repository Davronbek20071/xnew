from __future__ import annotations

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Query, Request
from loguru import logger

from app.bot.dispatcher import create_bot_and_dispatcher
from app.config import get_config
from app.db.repository import SupabaseRepository
from app.logger import setup_logger
from app.security import SessionCipher

app = FastAPI(title="X Obunachi Admin Bot (Vercel Webhook)")


@app.get("/")
@app.get("/api/health")
async def healthcheck() -> dict:
    return {"status": "ok", "service": "x-obunachi-multi-userbot"}


@app.get("/api/setup-webhook")
async def setup_webhook(secret: str = Query(..., description="WEBHOOK_SECRET qiymati")) -> dict:
    """Telegram webhook'ni bir marta bosishda ulash uchun endpoint."""
    config = get_config()
    setup_logger(config.log_level)

    if secret != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Noto'g'ri secret kalit.")
    if not config.webhook_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_BASE_URL sozlanmagan.")

    repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
    cipher = SessionCipher(config.session_encryption_key)
    bot, _ = create_bot_and_dispatcher(config=config, repo=repo, cipher=cipher)
    try:
        await bot.set_webhook(
            url=config.webhook_url,
            secret_token=config.webhook_secret,
            drop_pending_updates=True,
        )
        return {"ok": True, "webhook_url": config.webhook_url}
    finally:
        await bot.session.close()
        await repo.close()


@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Telegram Admin Bot webhook qabul qiluvchisi (Vercel serverless)."""
    config = get_config()
    setup_logger(config.log_level)

    if x_telegram_bot_api_secret_token != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden: invalid webhook secret")

    payload = await request.json()
    update = Update.model_validate(payload)

    repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
    cipher = SessionCipher(config.session_encryption_key)
    bot, dp = create_bot_and_dispatcher(config=config, repo=repo, cipher=cipher)

    try:
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as exc:
        logger.exception("Webhook qayta ishlashda xatolik: {}", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        await bot.session.close()
        await repo.close()
