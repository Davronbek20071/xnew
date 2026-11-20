from __future__ import annotations

import traceback

import httpx
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
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


@app.get("/api/cron-trigger")
async def cron_trigger(
    secret: str = Query(..., description="WEBHOOK_SECRET qiymati"),
    workflow: str = Query(default="worker.yml", description="Workflow fayl nomi"),
    force: str = Query(default="false", description="Cooldown'ni chetlab o'tish: true/false"),
):
    """Tashqi tekin cron servisi (masalan, cron-job.org) orqali GitHub Actions'ni aniq vaqtda ishga tushiradi."""
    try:
        config = get_config()
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    if secret != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Noto'g'ri secret kalit.")
    if not config.github_token or not config.github_repo:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "GITHUB_TOKEN yoki GITHUB_REPO sozlanmagan."},
        )

    url = (
        f"https://api.github.com/repos/{config.github_repo.strip('/')}"
        f"/actions/workflows/{workflow}/dispatches"
    )
    headers = {
        "Authorization": f"Bearer {config.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body: dict = {"ref": "main"}
    if workflow == "worker.yml":
        body["inputs"] = {"force": "true" if force.lower() == "true" else "false"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=body)

    if resp.status_code == 204:
        return {"ok": True, "triggered": workflow, "force": force}
    return JSONResponse(
        status_code=resp.status_code,
        content={"ok": False, "error": resp.text[:300]},
    )


@app.get("/api/setup-webhook")
async def setup_webhook(secret: str = Query(..., description="WEBHOOK_SECRET qiymati")):
    """Telegram webhook'ni bir marta bosishda ulash uchun endpoint."""
    try:
        config = get_config()
        setup_logger(config.log_level)
    except Exception as exc:
        print(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "stage": "config",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    if secret != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Noto'g'ri secret kalit.")
    if not config.webhook_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_BASE_URL sozlanmagan.")

    repo = None
    bot = None
    try:
        repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
        cipher = SessionCipher(config.session_encryption_key)
        bot, _ = create_bot_and_dispatcher(config=config, repo=repo, cipher=cipher)
        await bot.set_webhook(
            url=config.webhook_url,
            secret_token=config.webhook_secret,
            drop_pending_updates=True,
        )
        return {"ok": True, "webhook_url": config.webhook_url}
    except Exception as exc:
        print(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "stage": "setup_webhook",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        if bot is not None:
            await bot.session.close()
        if repo is not None:
            await repo.close()


@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Telegram Admin Bot webhook qabul qiluvchisi (Vercel serverless)."""
    try:
        config = get_config()
        setup_logger(config.log_level)
    except Exception as exc:
        print(traceback.format_exc())
        return {"ok": False, "stage": "config", "error": f"{type(exc).__name__}: {exc}"}

    if x_telegram_bot_api_secret_token != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden: invalid webhook secret")

    payload = await request.json()
    update = Update.model_validate(payload)

    repo = None
    bot = None
    try:
        repo = SupabaseRepository(config.supabase_url, config.supabase_service_role_key)
        cipher = SessionCipher(config.session_encryption_key)
        bot, dp = create_bot_and_dispatcher(config=config, repo=repo, cipher=cipher)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as exc:
        logger.exception("Webhook qayta ishlashda xatolik: {}", exc)
        print(traceback.format_exc())
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if bot is not None:
            await bot.session.close()
        if repo is not None:
            await repo.close()
