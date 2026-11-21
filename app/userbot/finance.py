from __future__ import annotations

import asyncio
import re
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


def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _format_amount(val: Decimal) -> str:
    """Masalan: Decimal('3.30') -> '3.3', Decimal('6.00') -> '6'."""
    s = f"{val.normalize():f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


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
    """1) Barcha akkauntlardan asosiy akkauntga pul o'tkazish (08:30 & 18:30 UZT)
    2) Asosiy akkauntda barcha balansga avto-buyurtma berish (09:00 & 19:00 UZT)
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

    async def _get_finance_params(self) -> Tuple[str, str, str, str]:
        """(target_bot, master_phone, master_bot_id, order_channel) ni oladi."""
        rows = await self.repo._request("GET", "settings", params={"select": "key,value"})
        raw = {r["key"]: r["value"] for r in (rows or [])}
        target_bot = (raw.get("target_bot_username") or self.config.default_target_bot).strip().lstrip("@")
        master_phone = (raw.get("master_phone") or DEFAULT_MASTER_PHONE).strip()
        master_bot_id = (raw.get("master_bot_id") or DEFAULT_MASTER_BOT_ID).strip()
        order_channel = (raw.get("order_channel") or DEFAULT_ORDER_CHANNEL).strip()
        if not order_channel.startswith("@") and "t.me/" not in order_channel:
            order_channel = f"@{order_channel}"
        return target_bot, master_phone, master_bot_id, order_channel

    # ========================================================================
    # 1-VAZIFA: BARCHA AKKAUNTLARDAN ASOSIY HISOBGA PUL O'TKAZISH (08:30, 18:30)
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

        # 1. /start
        await _send_and_wait(client, bot_entity, "/start")
        await human_sleep(1.5, 2.5)

        # 2. "Hisobim"
        hisob_btn = await find_recent_reply_button(client, bot_entity, ("hisobim", "кабинет", "баланс"))
        hisob_msg = await _send_and_wait(client, bot_entity, hisob_btn or "Hisobim")
        if not hisob_msg:
            return Decimal("0.00")

        balance, own_bot_id = parse_hisobim_info(hisob_msg.raw_text or "")
        if own_bot_id == receiver_bot_id:
            logger.info("[{}] Bot ID ({}) qabul qiluvchi bilan bir xil — o'tkazilmaydi.", account.phone, own_bot_id)
            return Decimal("0.00")

        if balance is None or balance <= Decimal("0"):
            await self.repo.update_account(account.id, {"balance": Decimal("0.00")})
            logger.info("[{}] O'tkazish uchun mablag' yo'q (balans: {}).", account.phone, balance)
            return Decimal("0.00")

        # 3. "Mablag' ko'chirish" inline tugmasini bosish
        transfer_coords = find_inline_callback_coords(
            hisob_msg, ("mablag' ko'chirish", "mablag kochirish", "ko'chirish", "перевести")
        )
        if not transfer_coords:
            logger.warning("[{}] 'Mablag' ko'chirish' tugmasi topilmadi.", account.phone)
            return Decimal("0.00")

        await human_sleep(1.5, 2.5)
        await click_inline_button(hisob_msg, transfer_coords[0], transfer_coords[1])
        id_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=hisob_msg.id)
        if not id_prompt:
            logger.warning("[{}] ID so'rov xabari kelmadi.", account.phone)
            return Decimal("0.00")

        # 4. Qabul qiluvchi bot ID sini yuborish (399380)
        await human_sleep(1.5, 2.5)
        confirm_msg = await _send_and_wait(client, bot_entity, receiver_bot_id)
        if not confirm_msg:
            return Decimal("0.00")

        # 5. "Tasdiqlash" inline tugmasini bosish
        confirm_coords = find_inline_callback_coords(confirm_msg, ("tasdiqlash", "tasdiq", "подтвер"))
        if not confirm_coords:
            logger.warning(
                "[{}] Qabul qiluvchi tasdiqlash tugmasi topilmadi: {}",
                account.phone,
                (confirm_msg.raw_text or "")[:120],
            )
            return Decimal("0.00")

        await human_sleep(1.5, 2.5)
        await click_inline_button(confirm_msg, confirm_coords[0], confirm_coords[1])
        amount_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=confirm_msg.id)
        if not amount_prompt:
            logger.warning("[{}] Summa kiritish xabari kelmadi.", account.phone)
            return Decimal("0.00")

        # Aniq ko'chiriladigan summani oxirgi xabardan qayta tekshiramiz
        fresh_bal, _ = parse_hisobim_info(amount_prompt.raw_text or "")
        send_val = fresh_bal if (fresh_bal is not None and fresh_bal > 0) else balance
        amount_str = _format_amount(send_val)

        # 6. Summani yuborish
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
    # 2-VAZIFA: ASOSIY AKKAUNTDA AVTO-BUYURTMA BERISH (09:00, 19:00)
    # ========================================================================
    async def run_auto_order(self) -> int:
        target_bot, master_phone, _, order_channel = await self._get_finance_params()
        master_digits = _digits_only(master_phone)
        accounts = await self.repo.list_accounts()

        master_acc = next(
            (a for a in accounts if _digits_only(a.phone) == master_digits),
            None,
        )
        if not master_acc:
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

        total_ordered = 0
        placed_orders: List[str] = []
        last_balance: Optional[Decimal] = None
        stop_note = ""

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await self.notifier.notify(
                    f"❌ <b>Avto-Buyurtma xatosi:</b> <code>{master_phone}</code> sessiyasi uzilgan!"
                )
                return 0

            bot_entity = await client.get_entity(target_bot)

            # Agar balans 1 martalik maks. limitdan (masalan 15 dona) katta bo'lsa,
            # balans 10 ₽ dan kam qolgunicha ketma-ket buyurtma beradi (maks 10 ta buyurtma)
            for _ in range(10):
                # 1. /start
                await _send_and_wait(client, bot_entity, "/start")
                await human_sleep(1.5, 2.5)

                # 2. "Buyurtma" reply tugmasi
                order_btn = await find_recent_reply_button(client, bot_entity, ("buyurtma", "заказ"))
                order_menu = await _send_and_wait(client, bot_entity, order_btn or "Buyurtma")
                if not order_menu:
                    stop_note = "'Buyurtma' menyusi ochilmadi."
                    break

                # 3. "Obunachi = 1 ₽" inline tugmasini bosish ("Tezkor obunachi" EMAS!)
                sub_coords = find_regular_subscriber_button(order_menu)
                if not sub_coords:
                    stop_note = "'Obunachi = 1 ₽' tugmasi topilmadi."
                    break

                await human_sleep(1.5, 2.5)
                await click_inline_button(order_menu, sub_coords[0], sub_coords[1])
                qty_prompt = await _wait_bot_reply_after(client, bot_entity, min_msg_id=order_menu.id)
                if not qty_prompt:
                    stop_note = "Miqdor so'rovi kelmadi."
                    break

                balance, min_qty, max_qty = parse_order_limits(qty_prompt.raw_text or "")
                last_balance = balance
                if balance is None:
                    stop_note = "Balans o'qilmadi."
                    break

                available_int = int(balance)
                if available_int < min_qty:
                    stop_note = (
                        f"Balans ({_format_amount(balance)} ₽) minimal {min_qty} ₽ dan kam."
                    )
                    break

                order_qty = min(available_int, max_qty) if max_qty else available_int
                logger.info(
                    "[{}] Buyurtma berilmoqda: {} dona (balans: {}, max: {}) -> {}",
                    master_acc.phone,
                    order_qty,
                    balance,
                    max_qty,
                    order_channel,
                )

                # 4. Obunachi sonini yuborish
                await human_sleep(1.5, 2.5)
                link_prompt = await _send_and_wait(client, bot_entity, str(order_qty))
                if not link_prompt:
                    stop_note = "Kanal havolasi so'rovi kelmadi."
                    break

                # 5. Kanal username'ini yuborish (@imkon7isbot)
                await human_sleep(1.5, 2.5)
                preview_msg = await _send_and_wait(client, bot_entity, order_channel)
                if not preview_msg:
                    stop_note = "Buyurtma tasdiqlash oynasi kelmadi."
                    break

                # 6. "Tasdiqlash" inline tugmasini bosish
                confirm_coords = find_inline_callback_coords(
                    preview_msg, ("tasdiqlash", "tasdiq", "подтвер")
                )
                if not confirm_coords:
                    stop_note = f"Tasdiqlash tugmasi topilmadi: {(preview_msg.raw_text or '')[:120]}"
                    break

                await human_sleep(1.5, 2.5)
                await click_inline_button(preview_msg, confirm_coords[0], confirm_coords[1])
                await asyncio.sleep(3.5)

                # Bot "Tasdiqlash" xabarini tahrirlaydi yoki yangi xabar yuboradi ("Qabul qilindi...")
                recent = await client.get_messages(bot_entity, limit=4)
                accepted_text = ""
                for m in recent:
                    if not getattr(m, "out", False) and (
                        "qabul qilindi" in normalize_button_text(m.raw_text or "")
                        or "buyurtma raqami" in normalize_button_text(m.raw_text or "")
                    ):
                        accepted_text = m.raw_text or ""
                        break

                order_id = parse_order_id(accepted_text) or "—"
                if accepted_text:
                    total_ordered += order_qty
                    remaining = max(Decimal("0.00"), balance - Decimal(order_qty))
                    last_balance = remaining
                    await self.repo.update_account(master_acc.id, {"balance": remaining})
                    await self.repo.log_stat(
                        account_id=master_acc.id,
                        action_type="info",
                        amount=Decimal(order_qty),
                        details=f"auto_order: #{order_id} | {order_qty} ta -> {order_channel}",
                    )
                    placed_orders.append(f"• Buyurtma <b>#{order_id}</b>: <b>{order_qty} ta</b>")
                    if int(remaining) < min_qty:
                        break
                    await human_sleep(3.0, 5.0)
                else:
                    stop_note = "Buyurtma qabul qilinganligi tasdiqlanmadi (bot adminligini tekshiring)."
                    break

        except Exception as exc:
            logger.exception("[{}] Avto-Buyurtmada xato: {}", master_acc.phone, exc)
            stop_note = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if placed_orders:
            bal_line = (
                f"💰 Qolgan balans: <b>{_format_amount(last_balance)} ₽</b>"
                if last_balance is not None
                else ""
            )
            await self.notifier.notify(
                f"🛒 <b>Avto-Buyurtma muvaffaqiyatli berildi!</b>\n"
                f"📱 Akkaunt: <code>{master_acc.phone}</code>\n"
                f"📢 Kanal: <code>{order_channel}</code>\n"
                f"👥 Jami buyurtma: <b>{total_ordered} ta obunachi</b>\n"
                f"{bal_line}\n\n" + "\n".join(placed_orders)
            )
        else:
            await self.notifier.notify(
                f"ℹ️ <b>Avto-Buyurtma berilmadi</b>\n"
                f"📱 Akkaunt: <code>{master_acc.phone}</code>\n"
                f"📢 Kanal: <code>{order_channel}</code>\n"
                f"📝 Sabab: {stop_note or 'Balans yetarli emas.'}"
            )

        return total_ordered
