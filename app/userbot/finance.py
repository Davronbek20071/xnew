from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.config import AppConfig
from app.db.models import AccountModel, AccountStatus
from app.db.repository import SupabaseRepository
from app.security import SessionCipher
from app.services.notifier import AdminNotifier
from app.userbot.client_factory import create_telegram_client
from app.userbot.parsers import normalize_button_text
from app.userbot.tasks.subscribe import (
    click_inline_button,
    find_inline_callback_coords,
    find_recent_reply_button,
    human_sleep,
)

# Standart qiymatlar (Supabase settings jadvalidan ham o'zgartirish mumkin)
DEFAULT_MASTER_PHONE = "+8801890592783"
DEFAULT_MASTER_BOT_ID = "399380"
DEFAULT_ORDER_CHANNEL = "@imkon7isbot"
DEFAULT_BACKUP_ORDER_CHANNEL = "@MemoraUzApp"
PENDING_ORDER_SETTING_KEY = "pending_auto_order"
LAST_ORDER_CHANNEL_SETTING_KEY = "last_ordered_channel"

DEAD_SESSION_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    SessionRevokedError,
)

_BALANCE_RE = re.compile(r"hisobingiz\s*:\s*([\d]+(?:[.,][\d]+)?)", re.IGNORECASE)
_BOT_ID_RE = re.compile(r"id\s*raqamingiz\s*:\s*(\d+)", re.IGNORECASE)
_MIN_ORDER_RE = re.compile(r"eng\s*kam\s*buyurtma\s*miqdori\s*:\s*(\d+)", re.IGNORECASE)
_MAX_ORDER_RE = re.compile(r"eng\s*ko[‘’ʻʼ'`´]?p\s*buyurtma\s*miqdori\s*:\s*(\d+)", re.IGNORECASE)
_ORDER_ID_RE = re.compile(r"buyurtma\s*raqami\s*:\s*(\d+)", re.IGNORECASE)


@dataclass
class _SingleOrderAttempt:
    status: str  # "accepted" | "busy" | "low_balance" | "error"
    ordered_qty: int = 0
    order_id: str = "—"
    balance: Optional[Decimal] = None
    min_qty: int = 10
    note: str = ""


def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _format_amount(val: Decimal) -> str:
    """Masalan: Decimal('3.30') -> '3.3', Decimal('6.00') -> '6'."""
    s = f"{val.normalize():f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _normalize_channel_ref(raw: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$", val, re.I)
    if m:
        return f"@{m.group(1)}"
    if not val.startswith("@") and "t.me/" not in val:
        return f"@{val}"
    return val


def parse_hisobim_info(text: str) -> Tuple[Optional[Decimal], Optional[str]]:
    """'Hisobim' xabaridan (balans, bot_id) ni ajratadi."""
    bal: Optional[Decimal] = None
    bot_id: Optional[str] = None
    m_bal = _BALANCE_RE.search(text or "")
    if m_bal:
        bal = Decimal(m_bal.group(1).replace(",", "."))
    m_id = _BOT_ID_RE.search(text or "")
    if m_id:
        bot_id = m_id.group(1)
    return bal, bot_id


def parse_order_limits(text: str) -> Tuple[Optional[Decimal], int, Optional[int]]:
    """'Obunachi = 1 ₽' javobidan (balans, min_qty, max_qty) ni ajratadi."""
    bal, _ = parse_hisobim_info(text)
    m_min = _MIN_ORDER_RE.search(text or "")
    m_max = _MAX_ORDER_RE.search(text or "")
    min_qty = int(m_min.group(1)) if m_min else 10
    max_qty = int(m_max.group(1)) if m_max else None
    return bal, min_qty, max_qty


def parse_order_id(text: str) -> Optional[str]:
    m = _ORDER_ID_RE.search(text or "")
    return m.group(1) if m else None


def is_unfinished_order_error(text: str) -> bool:
    """'Ushbu kanal uchun yakunlanmagan buyurtma bor' holatini aniqlaydi."""
    norm = normalize_button_text(text)
    return "yakunlanmagan buyurtma" in norm or "buyurtma yakunlanishini kuting" in norm


def find_regular_subscriber_button(msg: Any) -> Optional[Tuple[int, int, str]]:
    """'Obunachi = 1 ₽' tugmasini topadi ('Tezkor obunachi' ni ANIQ chetlab o'tadi!)."""
    if not msg:
        return None
    from app.userbot.tasks.subscribe import _is_inline_button, _iter_buttons

    for r_idx, c_idx, btn in _iter_buttons(msg):
        if not _is_inline_button(btn, msg):
            continue
        if getattr(btn, "url", None) or getattr(getattr(btn, "button", None), "url", None):
            continue
        btn_text = str(getattr(btn, "text", "") or "")
        norm = normalize_button_text(btn_text)
        if "obunachi" in norm and "tezkor" not in norm and "reaksiya" not in norm:
            return r_idx, c_idx, btn_text
    return None


async def _send_and_wait(
    client: TelegramClient, bot_entity: object, text: str, timeout: float = 25.0
) -> Optional[Any]:
    try:
        async with client.conversation(bot_entity, timeout=timeout) as conv:
            await conv.send_message(text)
            return await conv.get_response()
    except asyncio.TimeoutError:
        logger.warning("Bot '{}' xabariga javob kechiktirdi, oxirgi xabar olinmoqda...", text)
        msgs = await client.get_messages(bot_entity, limit=2)
        for m in msgs:
            if not getattr(m, "out", False):
                return m
        return None


async def _wait_bot_reply_after(
    client: TelegramClient,
    bot_entity: object,
    *,
    min_msg_id: int,
    max_wait_sec: float = 10.0,
) -> Optional[Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_sec
    while loop.time() < deadline:
        await asyncio.sleep(1.5)
        msgs = await client.get_messages(bot_entity, limit=4)
        for m in msgs:
            if m.id > min_msg_id and not getattr(m, "out", False):
                return m
    return None


class FinanceAutomation:
    """1) Barcha akkauntlardan asosiy akkauntga pul o'tkazish (08:37 & 18:37 UZT)
    2) Asosiy akkauntda avto-buyurtma berish (09:07 & 19:07 UZT):
       - Avval @imkon7isbot kanaliga buyurtma beradi;
       - Agar @imkon7isbot band bo'lsa, darhol zaxira @MemoraUzApp kanaliga beradi;
       - Agar ikkalasi ham band bo'lsa, navbatga qo'yib har 15-20 daqiqada qayta tekshiradi.
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

    async def _get_raw_settings(self) -> dict[str, str]:
        rows = await self.repo._request("GET", "settings", params={"select": "key,value"})
        return {r["key"]: r["value"] for r in (rows or [])}

    async def has_pending_order(self) -> bool:
        raw = await self._get_raw_settings()
        return raw.get(PENDING_ORDER_SETTING_KEY, "false").lower() == "true"

    async def _set_pending_order(self, value: bool) -> None:
        await self.repo.set_setting(PENDING_ORDER_SETTING_KEY, "true" if value else "false")

    async def _get_last_ordered_channel(self) -> str:
        raw = await self._get_raw_settings()
        return _normalize_channel_ref(raw.get(LAST_ORDER_CHANNEL_SETTING_KEY, ""))

    async def _set_last_ordered_channel(self, channel: str) -> None:
        await self.repo.set_setting(LAST_ORDER_CHANNEL_SETTING_KEY, _normalize_channel_ref(channel))

    async def _get_finance_params(self) -> Tuple[str, str, str, List[str]]:
        """(target_bot, master_phone, master_bot_id, [primary_channel, backup_channel]) ni oladi."""
        raw = await self._get_raw_settings()
        target_bot = (
            raw.get("target_bot_username") or self.config.default_target_bot
        ).strip().lstrip("@")
        master_phone = (raw.get("master_phone") or DEFAULT_MASTER_PHONE).strip()
        master_bot_id = (raw.get("master_bot_id") or DEFAULT_MASTER_BOT_ID).strip()

        primary = _normalize_channel_ref(raw.get("order_channel") or DEFAULT_ORDER_CHANNEL)
        backup = _normalize_channel_ref(
            raw.get("backup_order_channel") or DEFAULT_BACKUP_ORDER_CHANNEL
        )

        channels: List[str] = []
        for ch in (primary, backup):
            if ch and ch.lower() not in {c.lower() for c in channels}:
                channels.append(ch)
        return target_bot, master_phone, master_bot_id, channels

    # ========================================================================
    # 1-VAZIFA: BARCHA AKKAUNTLARDAN ASOSIY HISOBGA PUL O'TKAZISH
    # ========================================================================
    async def run_transfer_all(self) -> Decimal:
        target_bot, master_phone, master_bot_id, _ = await self._get_finance_params()
        master_digits = _digits_only(master_phone)
        accounts = await self.repo.list_active_accounts()

        total_transferred = Decimal("0.00")
        summary_lines: List[str] = []

        for acc in accounts:
            if _digits_only(acc.phone) == master_digits:
                logger.info("[{}] Asosiy qabul qiluvchi akkaunt — o'tkazib yuborildi.", acc.phone)
                continue

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
                if not await client.is_user_authorized():
                    summary_lines.append(f"❌ <code>{acc.phone}</code>: sessiya uzilgan")
                    continue

                transferred = await self._transfer_single_account(
                    client, acc, target_bot=target_bot, receiver_bot_id=master_bot_id
                )
                if transferred > 0:
                    total_transferred += transferred
                    summary_lines.append(
                        f"✅ <code>{acc.phone}</code> → <b>{_format_amount(transferred)} ₽</b>"
                    )
                else:
                    summary_lines.append(f"➖ <code>{acc.phone}</code>: balans 0 ₽")
            except DEAD_SESSION_ERRORS as exc:
                logger.error("[{}] O'tkazmada sessiya uzildi: {}", acc.phone, exc)
                await self.repo.update_account(
                    acc.id, {"status": AccountStatus.BANNED, "status_reason": str(exc)[:250]}
                )
                summary_lines.append(f"❌ <code>{acc.phone}</code>: sessiya uzildi")
            except FloodWaitError as exc:
                logger.warning("[{}] O'tkazmada FloodWait ({}s).", acc.phone, exc.seconds)
                summary_lines.append(f"⚠️ <code>{acc.phone}</code>: FloodWait {exc.seconds}s")
            except Exception as exc:
                logger.exception("[{}] O'tkazmada xato: {}", acc.phone, exc)
                summary_lines.append(f"⚠️ <code>{acc.phone}</code>: xato ({type(exc).__name__})")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            await human_sleep(2.0, 4.0)

        report_text = (
            f"💸 <b>Avtomatik Mablag‘ Yig‘ish Yakunlandi</b>\n"
            f"🎯 Qabul qiluvchi: <code>{master_phone}</code> (ID: <code>{master_bot_id}</code>)\n"
            f"💰 Jami o‘tkazildi: <b>{_format_amount(total_transferred)} ₽</b>\n\n"
            + ("\n".join(summary_lines) if summary_lines else "Faol akkauntlar topilmadi.")
        )
        await self.notifier.notify(report_text)
        return total_transferred

    async def _transfer_single_account(
        self,
        client: TelegramClient,
        account: AccountModel,
        *,
        target_bot: str,
        receiver_bot_id: str,
    ) -> Decimal:
        bot_entity = await client.get_entity(target_bot)

        await _send_and_wait(client, bot_entity, "/start")
        await human_sleep(1.5, 2.5)

        hisob_btn = await find_recent_reply_button(client, bot_entity, ("hisobim", "кабинет", "баланс"))
        hisob_msg = await _send_and_wait(client, bot_entity, hisob_btn or "Hisobim")
        if not hisob_msg:
            return Decimal("0.00")

        balance, own_bot_id = parse_hisobim_info(hisob_msg.raw_text or "")
        if own_bot_id == receiver_bot_id:
            return Decimal("0.00")

        if balance is None or balance <= Decimal("0"):
            await self.repo.update_account(account.id, {"balance": Decimal("0.00")})
            return Decimal("0.00")

        transfer_coords = find_inline_callback_coords(
            hisob_msg, ("mablag' ko'chirish", "mablag kochirish", "ko'chirish", "перевести")
        )
        if not transfer_coords:
            return Decimal("0.00")

        await human_sleep(1.5, 2.5)
        await click_inline_button(hisob_msg, transfer_coords[0], transfer_coords[1])
        id_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=hisob_msg.id)
        if not id_prompt:
            return Decimal("0.00")

        await human_sleep(1.5, 2.5)
        confirm_msg = await _send_and_wait(client, bot_entity, receiver_bot_id)
        if not confirm_msg:
            return Decimal("0.00")

        confirm_coords = find_inline_callback_coords(confirm_msg, ("tasdiqlash", "tasdiq", "подтвер"))
        if not confirm_coords:
            return Decimal("0.00")

        await human_sleep(1.5, 2.5)
        await click_inline_button(confirm_msg, confirm_coords[0], confirm_coords[1])
        amount_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=confirm_msg.id)
        if not amount_prompt:
            return Decimal("0.00")

        fresh_bal, _ = parse_hisobim_info(amount_prompt.raw_text or "")
        send_val = fresh_bal if (fresh_bal is not None and fresh_bal > 0) else balance
        amount_str = _format_amount(send_val)

        await human_sleep(1.5, 2.5)
        done_msg = await _send_and_wait(client, bot_entity, amount_str)
        done_text = (done_msg.raw_text or "") if done_msg else ""
        logger.info("[{}] O'tkazma javobi: {}", account.phone, done_text[:150])

        if "muvaffaqiyatli" in normalize_button_text(done_text) or "ko'chirildi" in normalize_button_text(done_text):
            rem_bal, _ = parse_hisobim_info(done_text)
            await self.repo.update_account(
                account.id, {"balance": rem_bal if rem_bal is not None else Decimal("0.00")}
            )
            await self.repo.log_stat(
                account_id=account.id,
                action_type="info",
                amount=send_val,
                details=f"transfer_out: {amount_str} ₽ -> ID {receiver_bot_id}",
            )
            return send_val

        return Decimal("0.00")

    # ========================================================================
    # 2-VAZIFA: ASOSIY AKKAUNTDA AVTO-BUYURTMA (@imkon7isbot -> @MemoraUzApp)
    # ========================================================================
    async def _attempt_order_for_channel(
        self,
        client: TelegramClient,
        bot_entity: object,
        master_acc: AccountModel,
        channel_ref: str,
    ) -> _SingleOrderAttempt:
        """Bitta aniq kanalga buyurtma berishga urinadi."""
        # 1. /start
        await _send_and_wait(client, bot_entity, "/start")
        await human_sleep(1.5, 2.5)

        # 2. "Buyurtma" reply tugmasi
        order_btn = await find_recent_reply_button(client, bot_entity, ("buyurtma", "заказ"))
        order_menu = await _send_and_wait(client, bot_entity, order_btn or "Buyurtma")
        if not order_menu:
            return _SingleOrderAttempt(status="error", note="'Buyurtma' menyusi ochilmadi.")

        # 3. "Obunachi = 1 ₽" inline tugmasini bosish ("Tezkor obunachi" EMAS!)
        sub_coords = find_regular_subscriber_button(order_menu)
        if not sub_coords:
            return _SingleOrderAttempt(status="error", note="'Obunachi = 1 ₽' tugmasi topilmadi.")

        await human_sleep(1.5, 2.5)
        await click_inline_button(order_menu, sub_coords[0], sub_coords[1])
        qty_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=order_menu.id)
        if not qty_prompt:
            return _SingleOrderAttempt(status="error", note="Miqdor so'rovi kelmadi.")

        balance, min_qty, max_qty = parse_order_limits(qty_prompt.raw_text or "")
        if balance is None:
            return _SingleOrderAttempt(status="error", note="Balans o'qilmadi.")

        if int(balance) < min_qty:
            return _SingleOrderAttempt(
                status="low_balance",
                balance=balance,
                min_qty=min_qty,
                note=f"Balans ({_format_amount(balance)} ₽) minimal {min_qty} ₽ dan kam.",
            )

        order_qty = min(int(balance), max_qty) if max_qty else int(balance)
        logger.info(
            "[{}] Buyurtma urinishi: {} dona (balans: {}, max: {}) -> {}",
            master_acc.phone,
            order_qty,
            balance,
            max_qty,
            channel_ref,
        )

        # 4. Obunachi sonini yuborish
        await human_sleep(1.5, 2.5)
        link_prompt = await _send_and_wait(client, bot_entity, str(order_qty))
        if not link_prompt:
            return _SingleOrderAttempt(status="error", balance=balance, note="Kanal havolasi so'rovi kelmadi.")

        # 5. Kanal username'ini yuborish
        await human_sleep(1.5, 2.5)
        preview_msg = await _send_and_wait(client, bot_entity, channel_ref)
        if not preview_msg:
            return _SingleOrderAttempt(status="error", balance=balance, note="Tasdiqlash oynasi kelmadi.")

        if is_unfinished_order_error(preview_msg.raw_text or ""):
            return _SingleOrderAttempt(status="busy", balance=balance, min_qty=min_qty)

        # 6. "Tasdiqlash" inline tugmasini bosish
        confirm_coords = find_inline_callback_coords(
            preview_msg, ("tasdiqlash", "tasdiq", "подтвер")
        )
        if not confirm_coords:
            return _SingleOrderAttempt(
                status="error",
                balance=balance,
                note=f"Tasdiqlash tugmasi topilmadi: {(preview_msg.raw_text or '')[:140]}",
            )

        await human_sleep(1.5, 2.5)
        await click_inline_button(preview_msg, confirm_coords[0], confirm_coords[1])

        # Bot "Tekshirilmoqda!" dan keyin adminlikni tekshirib 4-10 soniyada xabarni tahrirlaydi;
        # 15 soniyagacha har 1.5 soniyada tekshirib turamiz.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while loop.time() < deadline:
            await asyncio.sleep(1.5)
            recent = await client.get_messages(bot_entity, limit=5)
            for m in recent:
                if getattr(m, "out", False):
                    continue
                if m.id < preview_msg.id:
                    continue
                txt = m.raw_text or ""
                if is_unfinished_order_error(txt):
                    return _SingleOrderAttempt(status="busy", balance=balance, min_qty=min_qty)
                norm = normalize_button_text(txt)
                if "qabul qilindi" in norm or "buyurtma raqami" in norm:
                    order_id = parse_order_id(txt) or "—"
                    remaining = max(Decimal("0.00"), balance - Decimal(order_qty))
                    return _SingleOrderAttempt(
                        status="accepted",
                        ordered_qty=order_qty,
                        order_id=order_id,
                        balance=remaining,
                        min_qty=min_qty,
                    )

        return _SingleOrderAttempt(
            status="error",
            balance=balance,
            note=f"{channel_ref} uchun buyurtma tasdiqlanmadi (@XObunachiBot kanalda adminligini tekshiring).",
        )

    async def run_auto_order(self, *, is_retry: bool = False) -> int:
        target_bot, master_phone, _, channels = await self._get_finance_params()
        master_digits = _digits_only(master_phone)
        accounts = await self.repo.list_accounts()

        master_acc = next(
            (a for a in accounts if _digits_only(a.phone) == master_digits),
            None,
        )
        if not master_acc:
            if not is_retry:
                await self.notifier.notify(
                    f"❌ <b>Avto-Buyurtma xatosi:</b> Asosiy akkaunt (<code>{master_phone}</code>) bazada topilmadi!"
                )
            return 0

        proxy = await self.repo.get_proxy(master_acc.proxy_id) if master_acc.proxy_id else None
        raw_session = self.cipher.decrypt(master_acc.session_string)
        client = create_telegram_client(
            session_string=raw_session,
            api_id=master_acc.api_id,
            api_hash=master_acc.api_hash,
            proxy=proxy,
        )

        last_ordered_ch = await self._get_last_ordered_channel()
        primary_ch = channels[0] if channels else ""
        backup_blocked = (
            len(channels) > 1
            and bool(last_ordered_ch)
            and last_ordered_ch.lower() == channels[1].lower()
        )
        # Agar oxirgi buyurtma zaxira kanalga (@MemoraUzApp) berilgan bo'lsa,
        # bu safar FAQAT asosiy kanal (@imkon7isbot) ga ruxsat beramiz!
        allowed_channels = [primary_ch] if backup_blocked else list(channels)

        chosen_channel = ""
        is_backup_used = False
        busy_channels: List[str] = []
        final_attempt: Optional[_SingleOrderAttempt] = None

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await self._set_pending_order(False)
                await self.notifier.notify(
                    f"❌ <b>Avto-Buyurtma xatosi:</b> <code>{master_phone}</code> sessiyasi uzilgan!"
                )
                return 0

            bot_entity = await client.get_entity(target_bot)

            for idx, ch in enumerate(allowed_channels):
                attempt = await self._attempt_order_for_channel(client, bot_entity, master_acc, ch)
                final_attempt = attempt

                if attempt.status == "accepted":
                    chosen_channel = ch
                    is_backup_used = idx > 0
                    await self.repo.update_account(master_acc.id, {"balance": attempt.balance})
                    await self._set_last_ordered_channel(ch)
                    await self.repo.log_stat(
                        account_id=master_acc.id,
                        action_type="info",
                        amount=Decimal(attempt.ordered_qty),
                        details=f"auto_order: #{attempt.order_id} | {attempt.ordered_qty} ta -> {ch}",
                    )
                    # Qolgan balans hali ham >= min_qty bo'lsa, navbatda qoldiramiz
                    rem_int = int(attempt.balance) if attempt.balance is not None else 0
                    await self._set_pending_order(rem_int >= attempt.min_qty)
                    break

                if attempt.status == "busy":
                    busy_channels.append(ch)
                    if idx + 1 < len(allowed_channels):
                        logger.info(
                            "[{}] {} band — zaxira kanal ({}) tekshiriladi...",
                            master_acc.phone,
                            ch,
                            allowed_channels[idx + 1],
                        )
                        await human_sleep(2.0, 3.0)
                        continue
                    logger.info(
                        "[{}] {} band (zaxira bloklangan={}) — navbatga qo'yiladi.",
                        master_acc.phone,
                        ch,
                        backup_blocked,
                    )
                    break

                if attempt.status == "low_balance":
                    await self._set_pending_order(False)
                    break

                logger.warning("[{}] {} uchun buyurtma xatosi: {}", master_acc.phone, ch, attempt.note)
                if idx + 1 < len(allowed_channels):
                    await human_sleep(2.0, 3.0)
                    continue
                break

        except Exception as exc:
            logger.exception("[{}] Avto-Buyurtmada xato: {}", master_acc.phone, exc)
            final_attempt = _SingleOrderAttempt(status="error", note=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        # --- Adminga aniq va spamsiz hisobot ---
        if final_attempt and final_attempt.status == "accepted":
            bal_line = (
                f"💰 Qolgan balans: <b>{_format_amount(final_attempt.balance)} ₽</b>"
                if final_attempt.balance is not None
                else ""
            )
            backup_tag = (
                f" <i>(zaxira — {busy_channels[0]} band edi)</i>"
                if is_backup_used and busy_channels
                else ""
            )
            extra = (
                "\n⏳ Qolgan balans ham avvalgi buyurtma tugashi bilan avtomatik buyurtma qilinadi."
                if final_attempt.balance is not None and int(final_attempt.balance) >= final_attempt.min_qty
                else ""
            )
            await self.notifier.notify(
                f"🛒 <b>Avto-Buyurtma muvaffaqiyatli berildi!</b>\n"
                f"📱 Akkaunt: <code>{master_acc.phone}</code>\n"
                f"📢 Kanal: <code>{chosen_channel}</code>{backup_tag}\n"
                f"👥 Obunachi soni: <b>{final_attempt.ordered_qty} ta ({final_attempt.ordered_qty} ₽)</b>\n"
                f"🆔 Buyurtma raqami: <b>#{final_attempt.order_id}</b>\n"
                f"{bal_line}{extra}"
            )
            return final_attempt.ordered_qty

        if len(busy_channels) == len(allowed_channels) and busy_channels:
            await self._set_pending_order(True)
            if not is_retry:
                bal_str = (
                    f"{_format_amount(final_attempt.balance)} ₽"
                    if final_attempt and final_attempt.balance is not None
                    else "—"
                )
                if backup_blocked:
                    reason_text = (
                        f"<code>{primary_ch}</code> hali band. Oxirgi buyurtma zaxira "
                        f"<code>{channels[1]}</code> kanaliga berilgani uchun unga ketma-ket "
                        f"2-marta buyurtma berilmadi — faqat <code>{primary_ch}</code> bo‘shashi kutiladi."
                    )
                else:
                    ch_list = " va ".join(f"<code>{c}</code>" for c in busy_channels)
                    reason_text = f"{ch_list} kanallarida faol (yakunlanmagan) buyurtma mavjud."
                await self.notifier.notify(
                    f"⏳ <b>Avto-Buyurtma navbatga qo‘yildi!</b>\n"
                    f"💰 Hisobdagi mablag‘: <b>{bal_str}</b>\n"
                    f"📝 Sabab: {reason_text}\n"
                    f"🔄 Tizim har 15–20 daqiqada tekshirib, bo‘shashi bilanoq avtomatik buyurtma beradi!"
                )
            return 0

        if not is_retry:
            note = final_attempt.note if final_attempt else "Balans yetarli emas."
            await self.notifier.notify(
                f"ℹ️ <b>Avto-Buyurtma berilmadi</b>\n"
                f"📱 Akkaunt: <code>{master_acc.phone}</code>\n"
                f"📝 Sabab: {note}"
            )
        return 0
