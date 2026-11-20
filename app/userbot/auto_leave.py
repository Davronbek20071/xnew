from __future__ import annotations

import asyncio
import random

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import LeaveChannelRequest

from app.db.models import AccountModel
from app.db.repository import SupabaseRepository
from app.services.notifier import AdminNotifier


class AutoLeaveWorker:
    """16 kun (384 soat) to'lgan kanallardan avtomatik chiqib ketuvchi modul.

    X Obunachi [Bot] ning 15 kunlik jarima qoidasidan himoya qiladi va
    500 ta kanal limitida doim bo'sh joy saqlab turadi.
    """

    def __init__(self, *, repo: SupabaseRepository, notifier: AdminNotifier) -> None:
        self.repo = repo
        self.notifier = notifier

    async def leave_due_for_account(
        self, client: TelegramClient, account: AccountModel, max_per_run: int = 20
    ) -> int:
        due_subs = await self.repo.list_due_subscriptions(
            account_id=account.id, limit=max_per_run
        )
        if not due_subs:
            return 0

        logger.info(
            "[{}] Auto-Leave: {} ta muddati tugagan kanal topildi.",
            account.phone,
            len(due_subs),
        )
        left_count = 0
        for sub in due_subs:
            target_ref = sub.chat_id or sub.username_or_link
            try:
                entity = await client.get_entity(target_ref)
                await client(LeaveChannelRequest(entity))
                await self.repo.mark_subscription_left(sub.id)
                await self.repo.log_stat(
                    account_id=account.id,
                    action_type="leave",
                    details=f"16 kun o'tdi, chiqildi: {sub.username_or_link}",
                )
                left_count += 1
                logger.info("[{}] Kanaldan chiqildi: {}", account.phone, sub.username_or_link)
            except FloodWaitError as exc:
                logger.warning(
                    "[{}] Auto-Leave FloodWait: {}s. Keyingi siklda davom etiladi.",
                    account.phone,
                    exc.seconds,
                )
                break
            except Exception as exc:
                # Kanal allaqachon o'chirilgan yoki chiqib ketilgan bo'lsa bazada yopamiz
                logger.warning(
                    "[{}] Kanaldan chiqishda xato ({}): {}. Bazada yopildi.",
                    account.phone,
                    sub.username_or_link,
                    exc,
                )
                await self.repo.mark_subscription_left(sub.id)

            await asyncio.sleep(random.uniform(2.0, 5.0))

        return left_count
