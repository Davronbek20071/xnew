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

EARN_MENU_KEYWORDS = ("mablag", "yig'ish", "yigish", "pul ishlash", "заработ")
SUBSCRIBE_BTN_KEYWORDS = ("obuna bo'lish", "obuna bolish", "kanalga obuna", "подписаться")
HERE_SUB_KEYWORDS = ("shu yerda obuna", "shu yerda", "подписаться здесь")
CONFIRM_SUB_KEYWORDS = (
    "obuna bo'ldim",
    "obuna boldim",
    "tasdiq",
    "tekshir",
    "подписался",
    "проверить",
)


async def human_sleep(min_sec: float, max_sec: float) -> None:
    """Inson harakatiga o'xshash tasodifiy pauza."""
    delay = random.uniform(min_sec, max(min_sec, max_sec))
    await asyncio.sleep(delay)


def _iter_buttons(msg: Any) -> List[Tuple[int, int, Any]]:
    """Telethon MessageButton (msg.buttons) yoki raw reply_markup.rows dan barcha tugmalarni yig'adi."""
    result: List[Tuple[int, int, Any]] = []
    if not msg:
        return result

    buttons_2d = getattr(msg, "buttons", None)
    if buttons_2d:
        for r_idx, row in enumerate(buttons_2d):
            for c_idx, btn in enumerate(row):
                result.append((r_idx, c_idx, btn))
        return result

    markup = getattr(msg, "reply_markup", None)
    rows = getattr(markup, "rows", None) if markup else None
    if rows:
        for r_idx, row in enumerate(rows):
            for c_idx, btn in enumerate(getattr(row, "buttons", None) or []):
                result.append((r_idx, c_idx, btn))
    return result


def _is_inline_button(btn: Any, msg: Any) -> bool:
    """Tugma inline (callback yoki URL) ekanini Telethon versiyasidan qat'i nazar aniqlaydi."""
    if getattr(btn, "url", None) or getattr(btn, "data", None) is not None:
        return True
    raw_btn = getattr(btn, "button", btn)
    if getattr(raw_btn, "url", None) or getattr(raw_btn, "data", None) is not None:
        return True
    markup = getattr(msg, "reply_markup", None)
    return markup is not None and "inline" in type(markup).__name__.lower()


def _is_reply_keyboard(msg: Any) -> bool:
    markup = getattr(msg, "reply_markup", None) if msg else None
    if markup is None:
        return False
    name = type(markup).__name__.lower()
    return "keyboard" in name and "inline" not in name


def extract_inline_urls(msg: Any) -> List[str]:
    urls: List[str] = []
    for _, _, btn in _iter_buttons(msg):
        url = getattr(btn, "url", None) or getattr(getattr(btn, "button", None), "url", None)
        if url:
            urls.append(str(url))
    return urls


def extract_button_texts(msg: Any) -> List[str]:
    texts: List[str] = []
    for _, _, btn in _iter_buttons(msg):
        text = getattr(btn, "text", None)
        if text:
            texts.append(str(text))
    return texts


def find_inline_callback_coords(
    msg: Any, keywords: Iterable[str]
) -> Optional[Tuple[int, int, str]]:
    """Inline tugmalar ichidan mos kalit so'zli callback tugma koordinatasini (row, col, text) topadi."""
    if not msg:
        return None
    kw_list = [normalize_button_text(k) for k in keywords]
    for r_idx, c_idx, btn in _iter_buttons(msg):
        if not _is_inline_button(btn, msg):
            continue
        url = getattr(btn, "url", None) or getattr(getattr(btn, "button", None), "url", None)
        if url:
            continue
        btn_text = str(getattr(btn, "text", "") or "")
        btn_norm = normalize_button_text(btn_text)
        if any(k in btn_norm for k in kw_list):
            return r_idx, c_idx, btn_text
    return None


async def click_inline_button(msg: Any, r_idx: int, c_idx: int) -> Any:
    """Telethon MessageButton.click() yoki msg.click(r, c) orqali ishonchli bosadi."""
    buttons_2d = getattr(msg, "buttons", None)
    if buttons_2d and r_idx < len(buttons_2d) and c_idx < len(buttons_2d[r_idx]):
        btn = buttons_2d[r_idx][c_idx]
        if hasattr(btn, "click"):
            return await btn.click()
    return await msg.click(r_idx, c_idx)


def find_reply_button_text(msg: Any, keywords: Iterable[str]) -> Optional[str]:
    """Reply klaviaturadan mos tugma matnini (aynan o'zini) topadi."""
    if not _is_reply_keyboard(msg):
        return None
    kw_list = [normalize_button_text(k) for k in keywords]
    for _, _, btn in _iter_buttons(msg):
        btn_text = str(getattr(btn, "text", "") or "")
        btn_norm = normalize_button_text(btn_text)
        if any(k in btn_norm for k in kw_list):
            return btn_text
    return None


async def find_recent_reply_button(
    client: TelegramClient,
    bot_entity: object,
    keywords: Iterable[str],
    limit: int = 8,
) -> Optional[str]:
    """Botning oxirgi xabarlaridagi eng so'nggi ReplyKeyboard ichidan kerakli tugmani topadi."""
    msgs = await client.get_messages(bot_entity, limit=limit)
    for m in msgs:
        if _is_reply_keyboard(m):
            found = find_reply_button_text(m, keywords)
            if found:
                return found
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

    async def _click_here_and_get_task(
        self,
        client: TelegramClient,
        bot_entity: object,
        intro_msg: Any,
        here_coords: Tuple[int, int, str],
        account_phone: str,
    ) -> Tuple[Optional[Any], str]:
        """'Shu yerda obuna' tugmasini bosadi.

        Qaytaradi: (yangi_topshiriq_xabari_yoki_None, callback_alert_matni)
        """
        r_idx, c_idx, btn_label = here_coords
        latest_before = await client.get_messages(bot_entity, limit=1)
        last_id_before = latest_before[0].id if latest_before else intro_msg.id

        logger.info("[{}] '{}' inline tugmasi bosilmoqda...", account_phone, btn_label)
        callback_answer = await click_inline_button(intro_msg, r_idx, c_idx)
        alert_text = getattr(callback_answer, "message", "") or ""
        if alert_text:
            logger.info("[{}] 'Shu yerda obuna' javobi: {}", account_phone, alert_text)
            if is_no_more_channels(alert_text):
                return None, alert_text

        next_msg = await self._wait_for_new_task_message(
            client, bot_entity, min_msg_id=last_id_before, max_wait_sec=9.0
        )
        return next_msg, alert_text

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

        # 1. "Mablag' yig'ish" -> "Obuna bo'lish" menyusiga kirish
        sub_btn = await find_recent_reply_button(client, bot_entity, SUBSCRIBE_BTN_KEYWORDS)
        menu_msg = None

        if not sub_btn:
            earn_btn = await find_recent_reply_button(client, bot_entity, EARN_MENU_KEYWORDS)
            earn_text = earn_btn or "Mablag‘ yig‘ish"
            logger.info("[{}] '{}' menyusiga kirilmoqda...", account.phone, earn_text)
            menu_msg = await self._send_and_wait_reply(client, bot_entity, earn_text)
            await human_sleep(1.5, 3.0)
            if menu_msg:
                sub_btn = find_reply_button_text(menu_msg, SUBSCRIBE_BTN_KEYWORDS)

        task_intro_msg = None
        if sub_btn:
            logger.info("[{}] '{}' tugmasi bosilmoqda...", account.phone, sub_btn)
            task_intro_msg = await self._send_and_wait_reply(client, bot_entity, sub_btn)
        elif menu_msg is not None:
            inline_sub = find_inline_callback_coords(menu_msg, SUBSCRIBE_BTN_KEYWORDS)
            if inline_sub:
                logger.info("[{}] Inline '{}' bosilmoqda...", account.phone, inline_sub[2])
                await click_inline_button(menu_msg, inline_sub[0], inline_sub[1])
                task_intro_msg = await self._wait_for_new_task_message(
                    client, bot_entity, min_msg_id=menu_msg.id, max_wait_sec=8.0
                )
            else:
                task_intro_msg = menu_msg

        if not task_intro_msg:
            result.stopped_reason = "no_reply_to_subscribe_menu"
            return result

        if is_no_more_channels(task_intro_msg.raw_text):
            result.stopped_reason = "no_more_channels"
            return result

        # 2. "Shu yerda obuna" inline tugmasini bosish
        here_coords = find_inline_callback_coords(task_intro_msg, HERE_SUB_KEYWORDS)
        logger.info(
            "[{}] Intro xabardagi tugmalar: {}",
            account.phone,
            extract_button_texts(task_intro_msg),
        )

        current_msg = None
        if here_coords:
            await human_sleep(1.5, 3.0)
            current_msg, alert_text = await self._click_here_and_get_task(
                client, bot_entity, task_intro_msg, here_coords, account.phone
            )
            if is_no_more_channels(alert_text):
                logger.info("[{}] Obuna bo'lish uchun kanallar qolmadi.", account.phone)
                result.stopped_reason = "no_more_channels"
                return result
        else:
            # Agar bot to'g'ridan-to'g'ri kanal kartochkasini yuborgan bo'lsa
            if find_inline_callback_coords(task_intro_msg, CONFIRM_SUB_KEYWORDS):
                current_msg = task_intro_msg
            else:
                logger.warning(
                    "[{}] 'Shu yerda obuna' ham, 'Obuna bo'ldim' ham topilmadi.", account.phone
                )
                result.stopped_reason = "no_here_button"
                return result

        # 3. Vazifalar sikli (while): kanalga obuna -> "Obuna bo'ldim" -> keyingi kanal
        iterations = 0
        while current_msg and iterations < max_tasks:
            iterations += 1
            msg_text = current_msg.raw_text or ""
            if is_no_more_channels(msg_text):
                logger.info("[{}] Mavjud kanallar tugadi.", account.phone)
                result.stopped_reason = "no_more_channels"
                break

            confirm_coords = find_inline_callback_coords(current_msg, CONFIRM_SUB_KEYWORDS)
            if not confirm_coords:
                logger.info(
                    "[{}] Xabarda 'Obuna bo'ldim' tugmasi yo'q (tugmalar: {}). Sikl yakunlandi.",
                    account.phone,
                    extract_button_texts(current_msg),
                )
                result.stopped_reason = "no_confirm_button"
                break

            urls = extract_inline_urls(current_msg)
            targets = extract_all_telegram_targets(urls, fallback_text=msg_text)
            if not targets:
                logger.info("[{}] Kanal havolasi topilmadi, sikl to'xtatildi.", account.phone)
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
                    await click_inline_button(current_msg, skip_coords[0], skip_coords[1])
                    current_msg = await self._wait_for_new_task_message(
                        client, bot_entity, min_msg_id=current_msg.id, max_wait_sec=8.0
                    )
                    continue
                result.stopped_reason = "invalid_channel_no_skip"
                break

            # Insoniy pauza (sozlamadagi min..max soniya)
            await human_sleep(delay_min, delay_max)

            # "Obuna bo'ldim" inline tugmasini bosish
            prev_id = current_msg.id
            logger.info("[{}] '{}' tugmasi bosilmoqda...", account.phone, confirm_coords[2])
            callback_answer = await click_inline_button(
                current_msg, confirm_coords[0], confirm_coords[1]
            )
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

            # Bot avtomatik yuboradigan keyingi kanal kartochkasini kutish
            next_msg = await self._wait_for_new_task_message(
                client, bot_entity, min_msg_id=prev_id, max_wait_sec=8.0
            )
            if next_msg:
                current_msg = next_msg
                continue

            # Agar bot avtomatik keyingi kanalni yubormasa — "Shu yerda obuna" ni bosib tekshiramiz
            if here_coords:
                next_msg, alert_after = await self._click_here_and_get_task(
                    client, bot_entity, task_intro_msg, here_coords, account.phone
                )
                if is_no_more_channels(alert_after):
                    logger.info("[{}] Barcha mavjud kanallar tugadi.", account.phone)
                    result.stopped_reason = "no_more_channels"
                    break
                if next_msg:
                    current_msg = next_msg
                    continue

            result.stopped_reason = "no_more_channels"
            break

        return result

    async def _wait_for_new_task_message(
        self,
        client: TelegramClient,
        bot_entity: object,
        *,
        min_msg_id: int,
        max_wait_sec: float = 8.0,
    ) -> Optional[Any]:
        """min_msg_id dan keyin bot yuborgan yangi kanal kartochkasini (yoki 'tugadi' xabarini) kutadi."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_sec
        while loop.time() < deadline:
            await asyncio.sleep(1.5)
            msgs = await client.get_messages(bot_entity, limit=4)
            for m in msgs:
                if m.id <= min_msg_id:
                    continue
                # O'zimiz yuborgan xabarlarni o'tkazib yuboramiz
                if getattr(m, "out", False):
                    continue
                if find_inline_callback_coords(m, CONFIRM_SUB_KEYWORDS) or is_no_more_channels(
                    m.raw_text or ""
                ):
                    return m
        return None
