from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Set

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetParticipantRequest, LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest

from app.db.models import AccountModel
from app.db.repository import SupabaseRepository
from app.services.notifier import AdminNotifier


class AutoLeaveWorker:
    """16 kunlik (384 soat) kanal va guruhlardan avtomatik chiqish moduli.

    Alohida `auto_leave.yml` workflow orqali ishlaydi (asosiy obuna siklini sekinlashtirmaydi!).
    2 bosqichda ishlaydi:
    1. Supabase `subscriptions` jadvalidagi muddati 16 kun to'lgan obunalardan chiqadi.
    2. `scan_dialogs=True` bo'lganda akkauntdagi Telegram kanal va guruhlarni tekshirib,
       16+ kun to'lganlarini xavfsiz limit va pauza bilan tark etadi.
    """

    def __init__(self, *, repo: SupabaseRepository, notifier: AdminNotifier) -> None:
        self.repo = repo
        self.notifier = notifier

    async def leave_due_for_account(
        self,
        client: TelegramClient,
        account: AccountModel,
        *,
        leave_days: int = 16,
        max_per_run: int = 25,
        scan_dialogs: bool = True,
        max_scan_dialogs: int = 80,
    ) -> int:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max(15, leave_days))
        left_count = 0
        already_handled_ids: Set[int] = set()

        # ------------------------------------------------------------------
        # 1-BOSQICH: Supabase bazasidagi muddati tugagan (16+ kun) obunalar
        # ------------------------------------------------------------------
        due_subs = await self.repo.list_due_subscriptions(
            account_id=account.id, limit=max_per_run
        )
        if due_subs:
            logger.info(
                "[{}] Auto-Leave (baza): {} ta muddati tugagan kanal topildi.",
                account.phone,
                len(due_subs),
            )
            for sub in due_subs:
                if left_count >= max_per_run:
                    break
                target_ref = sub.chat_id or sub.username_or_link
                try:
                    entity = await client.get_entity(target_ref)
                    await self._leave_entity(client, entity)
                    await self.repo.mark_subscription_left(sub.id)
                    await self.repo.log_stat(
                        account_id=account.id,
                        action_type="leave",
                        details=f"16 kun o'tdi (baza), chiqildi: {sub.username_or_link}",
                    )
                    if getattr(entity, "id", None):
                        already_handled_ids.add(int(entity.id))
                    left_count += 1
                    logger.info("[{}] Kanaldan chiqildi (baza): {}", account.phone, sub.username_or_link)
                except FloodWaitError as exc:
                    logger.warning(
                        "[{}] Auto-Leave FloodWait: {}s. Qolgani keyingi siklda davom etadi.",
                        account.phone,
                        exc.seconds,
                    )
                    return left_count
                except Exception as exc:
                    logger.warning(
                        "[{}] Kanaldan chiqishda xato ({}): {}. Bazada yopildi.",
                        account.phone,
                        sub.username_or_link,
                        exc,
                    )
                    await self.repo.mark_subscription_left(sub.id)

                await asyncio.sleep(random.uniform(2.0, 4.0))

        # ------------------------------------------------------------------
        # 2-BOSQICH: Telegram dialoglarni xavfsiz limit bilan skanerlash
        # ------------------------------------------------------------------
        checked_dialogs = 0
        if scan_dialogs and left_count < max_per_run:
            protected_ids = await self.repo.list_protected_chat_ids(account.id)
            try:
                async for dialog in client.iter_dialogs():
                    if left_count >= max_per_run or checked_dialogs >= max_scan_dialogs:
                        break
                    if not (dialog.is_channel or dialog.is_group):
                        continue

                    entity = dialog.entity
                    entity_id = getattr(entity, "id", None)
                    if entity_id is None:
                        continue
                    entity_id = int(entity_id)

                    # O'zi egasi yoki admini bo'lgan kanallarga tegmaymiz
                    if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
                        continue
                    if entity_id in protected_ids or entity_id in already_handled_ids:
                        continue

                    checked_dialogs += 1
                    try:
                        joined_at = await self._get_join_date(client, entity)
                    except FloodWaitError as exc:
                        logger.warning(
                            "[{}] Skanerlashda FloodWait ({}s) — to'xtatildi.",
                            account.phone,
                            exc.seconds,
                        )
                        break

                    # Telegram chekloviga tushmaslik uchun mikro-pauza
                    await asyncio.sleep(0.35)

                    if joined_at is None:
                        continue
                    if joined_at.tzinfo is None:
                        joined_at = joined_at.replace(tzinfo=timezone.utc)

                    if joined_at <= cutoff:
                        label = (
                            getattr(entity, "username", None)
                            or getattr(entity, "title", str(entity_id))
                        )
                        try:
                            await self._leave_entity(client, entity)
                            await self.repo.mark_subscription_left_by_chat_id(account.id, entity_id)
                            await self.repo.log_stat(
                                account_id=account.id,
                                action_type="leave",
                                details=(
                                    f"16+ kun to'lgan (skan), chiqildi: {label} "
                                    f"(qo'shilgan: {joined_at.strftime('%Y-%m-%d')})"
                                ),
                            )
                            left_count += 1
                            logger.info(
                                "[{}] Eski kanal tark etildi: {} (qo'shilgan: {})",
                                account.phone,
                                label,
                                joined_at.strftime("%Y-%m-%d"),
                            )
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                        except FloodWaitError as exc:
                            logger.warning(
                                "[{}] Auto-Leave FloodWait: {}s. Qolgani keyin davom etadi.",
                                account.phone,
                                exc.seconds,
                            )
                            break
                        except Exception as exc:
                            logger.debug(
                                "[{}] '{}' dan chiqishda xato: {}", account.phone, label, exc
                            )
            except Exception as exc:
                logger.warning("[{}] Dialoglarni skanerlashda xato: {}", account.phone, exc)

        await self.repo.log_stat(
            account_id=account.id,
            action_type="info",
            details=f"daily_auto_leave_scan: checked={checked_dialogs}, left={left_count}",
        )

        if left_count > 0:
            await self.notifier.notify(
                f"🧹 <b>16-kunlik Auto-Leave yakunlandi</b>\n"
                f"📱 Akkaunt: <code>{account.phone}</code>\n"
                f"🚪 Tark etilgan kanallar/guruhlar: <b>{left_count} ta</b>"
            )

        return left_count

    async def _get_join_date(self, client: TelegramClient, entity: object):
        """Akkaunt ushbu kanal/superguruhga qachon qo'shilganini aniq oladi."""
        if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
            try:
                res = await client(GetParticipantRequest(channel=entity, participant="me"))
                participant = getattr(res, "participant", None)
                return getattr(participant, "date", None)
            except FloodWaitError:
                raise
            except Exception:
                return None
        return getattr(entity, "date", None)

    async def _leave_entity(self, client: TelegramClient, entity: object) -> None:
        """Kanal, superguruh yoki oddiy guruhni toza tark etadi."""
        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            await client(LeaveChannelRequest(entity))
        else:
            await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
