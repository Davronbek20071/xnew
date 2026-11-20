from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Dict, List

from loguru import logger

from app.config import AppConfig
from app.db.models import AccountModel, AccountStatus
from app.db.repository import SupabaseRepository
from app.security import SessionCipher
from app.services.notifier import AdminNotifier
from app.userbot.auto_leave import AutoLeaveWorker
from app.userbot.automation import AutomationWorker, CycleReport
from app.userbot.client_factory import create_telegram_client


class AccountManager:
    """Ko'p akkauntli sessiyalarni boshqaruvchi markaziy menejer.

    Ikkala rejimda ishlaydi:
    1. `run_once_all()` — GitHub Actions / Cron uchun bir siklli ishga tushirish.
    2. `run_forever_daemon()` — VPS / Docker uchun 24/7 mustaqil asyncio.Task lar.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        repo: SupabaseRepository,
        notifier: AdminNotifier,
        cipher: SessionCipher,
    ) -> None:
        self.config = config
        self.repo = repo
        self.notifier = notifier
        self.cipher = cipher
        self._tasks: Dict[int, asyncio.Task] = {}

    async def _run_single_account_once(
        self,
        account: AccountModel,
        *,
        force: bool = False,
        sleep_on_flood: bool = False,
        jitter_max: float = 12.0,
    ) -> CycleReport:
        now = datetime.now(timezone.utc)

        # FloodWait tekshiruvi
        if account.status == AccountStatus.FLOOD_WAIT and account.flood_until:
            if now < account.flood_until.astimezone(timezone.utc):
                rem = int((account.flood_until.astimezone(timezone.utc) - now).total_seconds())
                logger.info("[{}] FloodWait davom etmoqda ({}s qoldi).", account.phone, rem)
                return CycleReport(
                    account_phone=account.phone, status="skipped_flood", note=f"{rem}s qoldi"
                )

        # Cooldown (next_run_at) tekshiruvi
        if not force and account.next_run_at:
            if now < account.next_run_at.astimezone(timezone.utc):
                rem = int((account.next_run_at.astimezone(timezone.utc) - now).total_seconds())
                logger.info(
                    "[{}] Keyingi siklgacha hali {} soniya bor. O'tkazib yuborildi.",
                    account.phone,
                    rem,
                )
                return CycleReport(
                    account_phone=account.phone, status="skipped_cooldown", note=f"{rem}s qoldi"
                )

        # Akkauntlar bir sekundda boshlanmasligi uchun tasodifiy kechikish (jitter)
        if jitter_max > 0:
            await asyncio.sleep(random.uniform(1.0, jitter_max))

        settings = await self.repo.get_settings(default_target_bot=self.config.default_target_bot)
        proxy = await self.repo.get_proxy(account.proxy_id) if account.proxy_id else None
        raw_session = self.cipher.decrypt(account.session_string)

        client = create_telegram_client(
            session_string=raw_session,
            api_id=account.api_id,
            api_hash=account.api_hash,
            proxy=proxy,
        )

        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("[{}] Sessiya avtorizatsiyadan chiqib ketgan!", account.phone)
                await self.repo.update_account(
                    account.id,
                    {
                        "status": AccountStatus.BANNED,
                        "status_reason": "Sessiya avtorizatsiyadan chiqqan (not authorized)",
                    },
                )
                await self.notifier.notify(
                    f"❌ <b>Sessiya uzildi!</b>\n"
                    f"📱 Akkaunt: <code>{account.phone}</code>\n"
                    f"Qaytadan Admin Panel orqali ulang."
                )
                return CycleReport(account_phone=account.phone, status="unauthorized")

            worker = AutomationWorker(
                repo=self.repo, notifier=self.notifier, settings=settings
            )
            return await worker.run_cycle(client, account, sleep_on_flood=sleep_on_flood)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def run_once_all(self, *, force: bool = False, max_concurrency: int = 5) -> List[CycleReport]:
        """Barcha faol akkauntlar uchun 1 ta to'liq siklni parallel ishlatadi (GitHub Actions uchun)."""
        accounts = await self.repo.list_active_accounts()
        if not accounts:
            logger.info("Faol akkauntlar topilmadi.")
            return []

        sem = asyncio.Semaphore(max_concurrency)

        async def _guarded(acc: AccountModel) -> CycleReport:
            async with sem:
                return await self._run_single_account_once(acc, force=force, sleep_on_flood=False)

        results = await asyncio.gather(*(_guarded(a) for a in accounts), return_exceptions=False)
        for rep in results:
            logger.info(
                "Yakun [{}]: status={} | majburiy={} | bonus={} | obuna={} (+{} ₽)",
                rep.account_phone,
                rep.status,
                rep.mandatory_joined,
                rep.bonus_earned,
                rep.channels_subscribed,
                rep.subscribe_earned,
            )
        return list(results)

    async def run_auto_leave_all(self) -> int:
        """Barcha faol akkauntlar uchun 16 kunlik muddati tugagan kanallardan chiqadi."""
        accounts = await self.repo.list_active_accounts()
        total_left = 0
        leave_worker = AutoLeaveWorker(repo=self.repo, notifier=self.notifier)

        for acc in accounts:
            proxy = await self.repo.get_proxy(acc.proxy_id) if acc.proxy_id else None
            raw_session = self.cipher.decrypt(acc.session_string)
            client = create_telegram_client(
                session_string=raw_session,
                api_id=acc.api_id,
                api_hash=acc.api_hash,
                proxy=proxy,
            )
            try:
                await client.connect()
                if await client.is_user_authorized():
                    total_left += await leave_worker.leave_due_for_account(client, acc)
            except Exception as exc:
                logger.warning("[{}] Auto-Leave ulanish xatosi: {}", acc.phone, exc)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        logger.info("Auto-Leave yakunlandi: jami {} ta kanaldan chiqildi.", total_left)
        return total_left

    async def _daemon_account_loop(self, account_id: int) -> None:
        """VPS 24/7 rejimi: bitta akkaunt uchun uzluksiz mustaqil sikl."""
        # Boshlang'ich tasodifiy kechikish
        await asyncio.sleep(random.uniform(2.0, 20.0))
        while True:
            acc = await self.repo.get_account(account_id)
            if not acc or acc.status in (AccountStatus.PAUSED, AccountStatus.BANNED):
                logger.info("Akkaunt #{} to'xtatildi, task yakunlanmoqda.", account_id)
                return

            await self._run_single_account_once(
                acc, force=False, sleep_on_flood=True, jitter_max=5.0
            )
            settings = await self.repo.get_settings(
                default_target_bot=self.config.default_target_bot
            )
            sleep_sec = random.uniform(
                settings.cycle_min_minutes * 60, settings.cycle_max_minutes * 60
            )
            logger.info("[{}] Keyingi sikl {:.1f} daqiqadan so'ng.", acc.phone, sleep_sec / 60)
            await asyncio.sleep(sleep_sec)

    async def run_forever_daemon(self) -> None:
        """VPS / Docker rejimi: har 60 soniyada bazadagi akkauntlarni sinxronlaydi."""
        logger.info("24/7 AccountManager daemon rejimi ishga tushdi.")
        last_leave_check = datetime.now(timezone.utc)

        while True:
            try:
                active_accounts = await self.repo.list_active_accounts()
                active_ids = {a.id for a in active_accounts}

                # Yangi faollashgan akkauntlar uchun task yaratish
                for acc in active_accounts:
                    task = self._tasks.get(acc.id)
                    if task is None or task.done():
                        self._tasks[acc.id] = asyncio.create_task(
                            self._daemon_account_loop(acc.id), name=f"acc-{acc.id}"
                        )

                # To'xtatilgan akkauntlar taskini bekor qilish
                for aid in list(self._tasks.keys()):
                    if aid not in active_ids:
                        t = self._tasks.pop(aid)
                        if not t.done():
                            t.cancel()

                # Har 1 soatda Auto-Leave tekshiruvi
                now = datetime.now(timezone.utc)
                if (now - last_leave_check).total_seconds() >= 3600:
                    await self.run_auto_leave_all()
                    last_leave_check = now
            except Exception as exc:
                logger.exception("Daemon sinxronizatsiya xatosi: {}", exc)

            await asyncio.sleep(60)
