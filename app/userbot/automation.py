from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional

from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.db.models import AccountModel, AccountStatus, DynamicSettings
from app.db.repository import SupabaseRepository
from app.services.notifier import AdminNotifier
from app.userbot.auto_leave import AutoLeaveWorker
from app.userbot.parsers import (
    extract_all_telegram_targets,
    is_mandatory_sub_message,
    parse_bonus_amount,
)
from app.userbot.tasks.base import BaseTaskHandler
from app.userbot.tasks.subscribe import (
    EARN_MENU_KEYWORDS,
    SubscribeTaskHandler,
    click_inline_button,
    extract_button_texts,
    extract_inline_urls,
    find_inline_callback_coords,
    find_recent_reply_button,
    find_reply_button_text,
    human_sleep,
    join_telegram_target,
)


@dataclass
class CycleReport:
    account_phone: str
    mandatory_joined: int = 0
    bonus_earned: Decimal = Decimal("0.00")
    channels_subscribed: int = 0
    subscribe_earned: Decimal = Decimal("0.00")
    channels_left: int = 0
    status: str = "ok"
    note: str = ""


class AutomationWorker:
    """Har bir akkaunt uchun X Obunachi [Bot] avtomatlashtirish algoritmi.

    Faqat tezkor 3-qadamli obuna/bonus siklini bajaradi (16 kunlik to'liq Auto-Leave
    alohida `auto_leave.yml` workflow'da ishlaydi, shuning uchun obuna sikli hech qachon
    sekinlashmaydi va timeout bo'lmaydi).
    """

    def __init__(
        self,
        *,
        repo: SupabaseRepository,
        notifier: AdminNotifier,
        settings: DynamicSettings,
        extra_handlers: Optional[List[BaseTaskHandler]] = None,
    ) -> None:
        self.repo = repo
        self.notifier = notifier
        self.settings = settings
        self.task_handlers: List[BaseTaskHandler] = [
            SubscribeTaskHandler(repo=repo, notifier=notifier, settings=settings)
        ]
        if extra_handlers:
            self.task_handlers.extend(extra_handlers)

    async def run_cycle(
        self,
        client: TelegramClient,
        account: AccountModel,
        *,
        sleep_on_flood: bool = False,
    ) -> CycleReport:
        """Bitta akkaunt uchun to'liq 3 qadamli siklni bajaradi."""
        report = CycleReport(account_phone=account.phone)
        target_username = self.settings.target_bot_username.lstrip("@")

        try:
            # Agar avvalgi FloodWait muddati tugagan bo'lsa, holatni darhol ACTIVE ga qaytaramiz
            if account.status == AccountStatus.FLOOD_WAIT:
                await self.repo.update_account(
                    account.id,
                    {
                        "status": AccountStatus.ACTIVE,
                        "status_reason": None,
                        "flood_until": None,
                    },
                )
                account.status = AccountStatus.ACTIVE

            bot_entity = await client.get_entity(target_username)

            # 1-QADAM: /start va Majburiy obunani tekshirish
            start_reply, mandatory_count = await self._step_start_and_mandatory_sub(
                client, account, bot_entity
            )
            report.mandatory_joined = mandatory_count

            await human_sleep(1.5, 3.0)

            # 2-QADAM: Kunlik Bonus (24 soatda 1 marta)
            bonus_val = await self._step_daily_bonus(client, account, bot_entity, start_reply)
            report.bonus_earned = bonus_val

            await human_sleep(1.5, 3.0)

            # 3-QADAM: Kanallarga obuna bo'lish (va boshqa ulangan vazifa modullari)
            for handler in self.task_handlers:
                task_res = await handler.execute(client, account, bot_entity)
                report.channels_subscribed += task_res.completed_count
                report.subscribe_earned += task_res.earned_amount
                if task_res.channels_limit_reached:
                    # 500 ta limitga yetgandagina tezkor baza bo'yicha tozalashni ishga tushiramiz
                    leave_worker = AutoLeaveWorker(repo=self.repo, notifier=self.notifier)
                    report.channels_left = await leave_worker.leave_due_for_account(
                        client,
                        account,
                        leave_days=self.settings.auto_leave_days,
                        max_per_run=15,
                        scan_dialogs=False,
                    )
                    report.status = "channels_limit"
                    report.note = "500 ta kanal limitiga yetdi; tezkor Auto-Leave bajarildi."
                    break

            # Keyingi sikl vaqtini tasodifiy (min..max daqiqa) belgilash
            next_minutes = random.uniform(
                self.settings.cycle_min_minutes, self.settings.cycle_max_minutes
            )
            now = datetime.now(timezone.utc)
            await self.repo.update_account(
                account.id,
                {
                    "status": AccountStatus.ACTIVE,
                    "status_reason": None,
                    "flood_until": None,
                    "last_active_at": now,
                    "next_run_at": now + timedelta(minutes=next_minutes),
                },
            )
            return report

        except FloodWaitError as exc:
            wait_sec = int(exc.seconds) + 10
            flood_until = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
            logger.warning(
                "[{}] FloodWaitError: {} soniya kutish kerak (gacha: {}).",
                account.phone,
                wait_sec,
                flood_until.isoformat(),
            )
            await self.repo.update_account(
                account.id,
                {
                    "status": AccountStatus.FLOOD_WAIT,
                    "status_reason": f"FloodWait {wait_sec}s",
                    "flood_until": flood_until,
                    "last_active_at": datetime.now(timezone.utc),
                    "next_run_at": flood_until,
                },
            )
            await self.repo.log_stat(
                account_id=account.id,
                action_type="error",
                details=f"FloodWaitError: {wait_sec} soniya.",
            )
            if exc.seconds > 600:
                await self.notifier.notify(
                    f"⚠️ <b>Uzoq FloodWait!</b>\n"
                    f"📱 Akkaunt: <code>{account.phone}</code>\n"
                    f"⏳ Kutish vaqti: <b>{wait_sec // 60} daqiqa</b> ({wait_sec} soniya)."
                )
            if sleep_on_flood:
                await asyncio.sleep(wait_sec)
            report.status = "flood_wait"
            report.note = f"FloodWait {wait_sec}s"
            return report

        except (
            AuthKeyUnregisteredError,
            UserDeactivatedBanError,
            UserDeactivatedError,
            SessionRevokedError,
        ) as exc:
            logger.error("[{}] Akkaunt bloklangan yoki sessiya uzilgan: {}", account.phone, exc)
            await self.repo.update_account(
                account.id,
                {
                    "status": AccountStatus.BANNED,
                    "status_reason": str(exc)[:250],
                },
            )
            await self.repo.log_stat(
                account_id=account.id,
                action_type="error",
                details=f"BANNED/REVOKED: {exc}",
            )
            await self.notifier.notify(
                f"❌ <b>Akkaunt to'xtatildi (Ban / Sessiya uzildi)!</b>\n"
                f"📱 Akkaunt: <code>{account.phone}</code>\n"
                f"📝 Sabab: <code>{str(exc)[:200]}</code>\n"
                f"Boshqa akkauntlar ishlashda davom etmoqda."
            )
            report.status = "banned"
            report.note = str(exc)
            return report

        except Exception as exc:
            logger.exception("[{}] Kutilmagan xatolik: {}", account.phone, exc)
            now = datetime.now(timezone.utc)
            await self.repo.update_account(
                account.id,
                {
                    "status": AccountStatus.ACTIVE,
                    "status_reason": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "flood_until": None,
                    "last_active_at": now,
                    "next_run_at": now + timedelta(minutes=10),
                },
            )
            await self.repo.log_stat(
                account_id=account.id,
                action_type="error",
                details=f"Kutilmagan xato: {type(exc).__name__}: {exc}",
            )
            report.status = "error"
            report.note = str(exc)
            return report

    async def _send_cmd_and_wait(
        self, client: TelegramClient, bot_entity: object, text: str, timeout: float = 25.0
    ) -> Optional[Any]:
        try:
            async with client.conversation(bot_entity, timeout=timeout) as conv:
                await conv.send_message(text)
                return await conv.get_response()
        except asyncio.TimeoutError:
            logger.warning("Bot '{}' buyrug'iga kechikdi, oxirgi xabar tekshirilmoqda...", text)
            msgs = await client.get_messages(bot_entity, limit=2)
            for m in msgs:
                if not getattr(m, "out", False):
                    return m
            return None

    async def _step_start_and_mandatory_sub(
        self, client: TelegramClient, account: AccountModel, bot_entity: object
    ) -> tuple[Optional[Any], int]:
        """1-qadam: /start yuborish va majburiy obuna kanallariga qo'shilish."""
        reply = await self._send_cmd_and_wait(client, bot_entity, "/start")
        if not reply:
            return None, 0

        joined_count = 0
        btn_texts = extract_button_texts(reply)
        if is_mandatory_sub_message(reply.raw_text or "", btn_texts):
            urls = extract_inline_urls(reply)
            targets = extract_all_telegram_targets(urls, fallback_text=reply.raw_text or "")
            logger.info(
                "[{}] Majburiy obuna aniqlandi: {} ta kanal.", account.phone, len(targets)
            )
            for target in targets:
                try:
                    chat_id, title, _ = await join_telegram_target(client, target)
                    await self.repo.add_subscription(
                        account_id=account.id,
                        chat_id=chat_id,
                        username_or_link=target.raw,
                        title=title,
                        is_mandatory=True,
                        leave_days=self.settings.auto_leave_days,
                    )
                    await self.repo.log_stat(
                        account_id=account.id,
                        action_type="mandatory_sub",
                        details=f"Majburiy obuna: {target.raw}",
                    )
                    joined_count += 1
                except Exception as exc:
                    logger.warning(
                        "[{}] Majburiy kanalga ({}) qo'shilishda xato: {}",
                        account.phone,
                        target.raw,
                        exc,
                    )
                await human_sleep(2.0, 4.0)

            confirm = find_inline_callback_coords(
                reply, ("tasdiq", "tekshir", "провер", "confirm")
            )
            if confirm:
                await click_inline_button(reply, confirm[0], confirm[1])
                await asyncio.sleep(2.5)
                msgs = await client.get_messages(bot_entity, limit=1)
                if msgs:
                    reply = msgs[0]

        return reply, joined_count

    async def _step_daily_bonus(
        self,
        client: TelegramClient,
        account: AccountModel,
        bot_entity: object,
        last_msg: Optional[Any],
    ) -> Decimal:
        """2-qadam: Kunlik Bonus (24 soatda 1 marta)."""
        now = datetime.now(timezone.utc)
        if account.last_bonus_at:
            elapsed = now - account.last_bonus_at.astimezone(timezone.utc)
            if elapsed < timedelta(hours=24):
                return Decimal("0.00")

        bonus_btn = find_reply_button_text(last_msg, ("bonus", "бонус"))
        if not bonus_btn:
            earn_btn = await find_recent_reply_button(client, bot_entity, EARN_MENU_KEYWORDS)
            if earn_btn:
                menu_msg = await self._send_cmd_and_wait(client, bot_entity, earn_btn)
                await human_sleep(1.5, 3.0)
                bonus_btn = find_reply_button_text(menu_msg, ("bonus", "бонус"))

        if not bonus_btn:
            bonus_btn = "Bonus"

        bonus_reply = await self._send_cmd_and_wait(client, bot_entity, bonus_btn)
        if not bonus_reply:
            return Decimal("0.00")

        amount = parse_bonus_amount(bonus_reply.raw_text or "")
        earned = amount if amount is not None else Decimal("0.00")

        update_data = {"last_bonus_at": now}
        if earned > 0:
            account.balance = account.balance + earned
            update_data["balance"] = account.balance
            logger.info("[{}] Kunlik bonus olindi: {} ₽", account.phone, earned)
        else:
            logger.info(
                "[{}] Bonus javobi: {}", account.phone, (bonus_reply.raw_text or "")[:120]
            )

        await self.repo.update_account(account.id, update_data)
        await self.repo.log_stat(
            account_id=account.id,
            action_type="bonus",
            amount=earned,
            details=(bonus_reply.raw_text or "")[:200],
        )
        return earned
