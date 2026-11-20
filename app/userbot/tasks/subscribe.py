from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from typing import Any, Iterable, List, Optional, Tuple

from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.db.models import AccountModel
from app.userbot.parsers import (
    TelegramTarget,
    extract_all_telegram_targets,
    is_no_more_channels,
    normalize_button_text,
    parse_balance_alert,
)
from app.userbot.tasks.base import BaseTaskHandler, TaskRunResult


async def human_sleep(min_sec: float, max_sec: float) -> None:
    """Inson harakatiga o'xshash tasodifiy pauza."""
    delay = random.uniform(min_sec, max(min_sec, max_sec))
    await asyncio.sleep(delay)


def _markup_rows(msg: Any) -> list:
    if not msg or not getattr(msg, "reply_markup", None):
        return []
    return list(getattr(msg.reply_markup, "rows", None) or [])


def _is_inline_markup(msg: Any) -> bool:
    markup = getattr(msg, "reply_markup", None) if msg else None
    if markup is None:
        return False
    return type(markup).__name__ == "ReplyInlineMarkup"


def _is_reply_keyboard(msg: Any) -> bool:
    markup = getattr(msg, "reply_markup", None) if msg else None
    if markup is None:
        return False
    return type(markup).__name__ == "ReplyKeyboardMarkup"


def extract_inline_urls(msg: Any) -> List[str]:
    urls: List[str] = []
    if not _is_inline_markup(msg):
        return urls
    for row in _markup_rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            url = getattr(btn, "url", None)
            if url:
                urls.append(str(url))
    return urls


def extract_button_texts(msg: Any) -> List[str]:
    texts: List[str] = []
    for row in _markup_rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            text = getattr(btn, "text", None)
            if text:
                texts.append(str(text))
    return texts


def find_inline_callback_coords(
    msg: Any, keywords: Iterable[str]
) -> Optional[Tuple[int, int, str]]:
    """Inline tugmalar ichidan mos kalit so'zli callback tugma koordinatasini (row, col) topadi."""
    if not _is_inline_markup(msg):
        return None
    kw_list = [normalize_button_text(k) for k in keywords]
    for r_idx, row in enumerate(_markup_rows(msg)):
        for c_idx, btn in enumerate(getattr(row, "buttons", None) or []):
            # Callback tugmalarda `data` atributi bo'ladi, URL tugmalarda esa `url`
            if getattr(btn, "data", None) is not None and not getattr(btn, "url", None):
                btn_text = str(getattr(btn, "text", "") or "")
                btn_norm = normalize_button_text(btn_text)
                if any(k in btn_norm for k in kw_list):
                    return r_idx, c_idx, btn_text
    return None


def find_reply_button_text(msg: Any, keywords: Iterable[str]) -> Optional[str]:
    """Reply klaviaturadan mos tugma matnini topadi."""
    if not _is_reply_keyboard(msg):
        return None
    kw_list = [normalize_button_text(k) for k in keywords]
    for row in _markup_rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            btn_text = str(getattr(btn, "text", "") or "")
            btn_norm = normalize_button_text(btn_text)
            if any(k in btn_norm for k in kw_list):
                return btn_text
    return None


async def mute_and_archive_peer(client: TelegramClient, entity: object) -> None:
    """Kanal bildirishnomalarini o'chiradi (mute) va Arxiv papkasiga o'tkazadi."""
    try:
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.functions.folders import EditPeerFoldersRequest
        from telethon.tl.types import (
            InputFolderPeer,
            InputNotifyPeer,
            InputPeerNotifySettings,
        )

        input_peer = await client.get_input_entity(entity)
        await client(
            UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=input_peer),
                settings=InputPeerNotifySettings(mute_until=2147483647, silent=True),
            )
        )
        await client(
            EditPeerFoldersRequest(
                folder_peers=[InputFolderPeer(peer=input_peer, folder_id=1)]
            )
        )
    except Exception as exc:
        logger.debug("Mute/Arxivlashda kichik xato (muhim emas): {}", exc)


async def join_telegram_target(
    client: TelegramClient, target: TelegramTarget
) -> Tuple[Optional[int], Optional[str], bool]:
    """Kanal/guruhga qo'shiladi.

    Qaytaradi: (chat_id, title, already_joined)
    ChannelsTooMuchError va FloodWaitError yuqoriga uzatiladi.
    """
    entity = None
    already_joined = False

    if target.kind == "private":
        try:
            updates = await client(ImportChatInviteRequest(target.value))
            chats = getattr(updates, "chats", None) or []
            if chats:
                entity = chats[0]
        except UserAlreadyParticipantError:
            already_joined = True
    else:
        entity = await client.get_entity(target.value)
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            already_joined = True

    if entity is not None:
        await mute_and_archive_peer(client, entity)
        chat_id = getattr(entity, "id", None)
        title = getattr(entity, "title", None) or getattr(entity, "username", None)
        return chat_id, title, already_joined

    return None, None, already_joined


class SubscribeTaskHandler(BaseTaskHandler):
    """3-qadam: X Obunachi [Bot] da kanallarga obuna bo'lish sikli."""

    name = "subscribe"

    async def _send_and_wait_reply(
        self, client: TelegramClient, bot_entity: object, text: str, timeout: float = 15.0
    ) -> Optional[Any]:
        try:
            async with client.conversation(bot_entity, timeout=timeout) as conv:
                await conv.send_message(text)
                return await conv.get_response()
        except asyncio.TimeoutError:
            logger.warning("Bot javob bermadi (timeout): {}", text)
            return None

    async def execute(
        self,
        client: TelegramClient,
        account: AccountModel,
        bot_entity: object,
    ) -> TaskRunResult:
        result = TaskRunResult()
        delay_min = self.settings.action_delay_min_sec
        delay_max = self.settings.action_delay_max_sec
        max_tasks = self.settings.max_tasks_per_cycle

        # 1. Reply menyudan "Obuna bo'lish" bo'limiga kirish
        latest_msgs = await client.get_messages(bot_entity, limit=2)
        last_msg = latest_msgs[0] if latest_msgs else None

        sub_btn = find_reply_button_text(last_msg, ("obuna bo'lish", "obuna bolish", "подписаться"))
        if not sub_btn:
            earn_btn = find_reply_button_text(last_msg, ("pul ishlash", "заработ"))
            if earn_btn:
                last_msg = await self._send_and_wait_reply(client, bot_entity, earn_btn)
                await human_sleep(1.5, 3.0)
                sub_btn = find_reply_button_text(
                    last_msg, ("obuna bo'lish", "obuna bolish", "подписаться")
                )

        target_text = sub_btn or "Obuna bo‘lish"
        task_intro_msg = await self._send_and_wait_reply(client, bot_entity, target_text)
        if not task_intro_msg:
            result.stopped_reason = "no_reply_to_subscribe_menu"
            return result

        if is_no_more_channels(task_intro_msg.raw_text):
            result.stopped_reason = "no_more_channels"
            return result

        # 2. "Shu yerda obuna" inline tugmasini bosish (agar mavjud bo'lsa)
        here_coords = find_inline_callback_coords(
            task_intro_msg, ("shu yerda obuna", "shu yerda", "подписаться здесь")
        )
        current_msg = task_intro_msg
        if here_coords:
            r_idx, c_idx, btn_label = here_coords
            logger.info("[{}] '{}' inline tugmasi bosilmoqda...", account.phone, btn_label)
            await human_sleep(1.5, 3.5)
            prev_text = current_msg.raw_text
            await current_msg.click(r_idx, c_idx)
            await asyncio.sleep(2.5)
            current_msg = await self._fetch_next_task_message(
                client, bot_entity, current_msg.id, prev_text
            )

        # 3. Vazifalar sikli (while)
        iterations = 0
        while current_msg and iterations < max_tasks:
            iterations += 1
            msg_text = current_msg.raw_text or ""
            if is_no_more_channels(msg_text):
                logger.info("[{}] Mavjud kanallar tugadi.", account.phone)
                result.stopped_reason = "no_more_channels"
                break

            urls = extract_inline_urls(current_msg)
            targets = extract_all_telegram_targets(urls, fallback_text=msg_text)
            if not targets:
                logger.info("[{}] Xabarda kanal havolasi topilmadi, sikl to'xtatildi.", account.phone)
                result.stopped_reason = "no_channel_target"
                break

            target = targets[0]
            logger.info("[{}] Kanalga qo'shilmoqda: {} ({})", account.phone, target.raw, target.kind)

            chat_id: Optional[int] = None
            title: Optional[str] = None
            try:
                chat_id, title, already = await join_telegram_target(client, target)
                if already:
                    logger.info("[{}] Akkaunt allaqachon a'zo: {}", account.phone, target.raw)
            except ChannelsTooMuchError:
                logger.warning("[{}] 500 ta kanal limitiga yetildi!", account.phone)
                result.channels_limit_reached = True
                result.stopped_reason = "channels_too_much"
                await self.repo.log_stat(
                    account_id=account.id,
                    action_type="error",
                    details="ChannelsTooMuchError: 500 ta kanal limitiga yetildi.",
                )
                await self.notifier.notify(
                    f"⚠️ <b>500 ta kanal limiti!</b>\n"
                    f"📱 Akkaunt: <code>{account.phone}</code>\n"
                    f"Obuna sikli to'xtatildi va Auto-Leave tekshiruvi ishga tushiriladi."
                )
                break
            except (
                InviteHashExpiredError,
                InviteHashInvalidError,
                UsernameInvalidError,
                UsernameNotOccupiedError,
                ChannelPrivateError,
            ) as exc:
                logger.warning("[{}] Yaroqsiz kanal havolasi ({}): {}", account.phone, target.raw, exc)
                await self.repo.log_stat(
                    account_id=account.id,
                    action_type="error",
                    details=f"Yaroqsiz havola {target.raw}: {exc}",
                )
                skip_coords = find_inline_callback_coords(
                    current_msg, ("o'tkazib", "otkazib", "keyingi", "пропустить", "skip")
                )
                if skip_coords:
                    await human_sleep(1.5, 3.0)
                    prev_text = current_msg.raw_text
                    await current_msg.click(skip_coords[0], skip_coords[1])
                    await asyncio.sleep(2.5)
                    current_msg = await self._fetch_next_task_message(
                        client, bot_entity, current_msg.id, prev_text
                    )
                    continue
                result.stopped_reason = "invalid_channel_no_skip"
                break

            # Insoniy pauza (3-6s yoki sozlamadagi pauza)
            await human_sleep(delay_min, delay_max)

            # "Obuna bo'ldim" inline tugmasini bosish
            confirm_coords = find_inline_callback_coords(
                current_msg,
                ("obuna bo'ldim", "obuna boldim", "tasdiq", "tekshir", "подписался", "проверить"),
            )
            if not confirm_coords:
                logger.warning("[{}] 'Obuna bo'ldim' tugmasi topilmadi.", account.phone)
                result.stopped_reason = "no_confirm_button"
                break

            prev_text = current_msg.raw_text
            prev_id = current_msg.id
            callback_answer = await current_msg.click(confirm_coords[0], confirm_coords[1])
            alert_text = getattr(callback_answer, "message", "") or ""
            logger.info("[{}] Tasdiqlash javobi: {}", account.phone, alert_text or "(bo'sh)")

            earned, new_balance = parse_balance_alert(alert_text)
            earned_val = earned if earned is not None else Decimal("0.00")

            # Bazaga obunani va balansni saqlash
            await self.repo.add_subscription(
                account_id=account.id,
                chat_id=chat_id,
                username_or_link=target.raw,
                title=title,
                is_mandatory=False,
                leave_days=self.settings.auto_leave_days,
            )
            update_payload = {}
            if new_balance is not None:
                update_payload["balance"] = new_balance
                account.balance = new_balance
            elif earned_val > 0:
                account.balance = account.balance + earned_val
                update_payload["balance"] = account.balance
            if update_payload:
                await self.repo.update_account(account.id, update_payload)

            await self.repo.log_stat(
                account_id=account.id,
                action_type="subscribe",
                amount=earned_val,
                details=f"{target.raw} | {alert_text}".strip(" |"),
            )

            result.completed_count += 1
            result.earned_amount += earned_val

            # Keyingi topshiriq xabarini kutish (bot yangi xabar yuboradi yoki eskisini tahrirlaydi)
            await asyncio.sleep(2.5)
            next_msg = await self._fetch_next_task_message(client, bot_entity, prev_id, prev_text)
            if not next_msg or (next_msg.id == prev_id and next_msg.raw_text == prev_text):
                result.stopped_reason = "no_next_message"
                break
            current_msg = next_msg

        return result

    async def _fetch_next_task_message(
        self,
        client: TelegramClient,
        bot_entity: object,
        prev_msg_id: int,
        prev_text: str,
    ) -> Optional[Any]:
        """Bot yuborgan yangi xabar yoki tahrirlangan xabarni oladi."""
        for _ in range(3):
            msgs = await client.get_messages(bot_entity, limit=2)
            if msgs:
                newest = msgs[0]
                if newest.id != prev_msg_id or newest.raw_text != prev_text:
                    return newest
            await asyncio.sleep(1.5)
        msgs = await client.get_messages(bot_entity, limit=1)
        return msgs[0] if msgs else None
