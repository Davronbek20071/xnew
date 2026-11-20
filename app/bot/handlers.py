from __future__ import annotations

import re
from datetime import timedelta, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app.bot.keyboards import (
    account_detail_kb,
    accounts_list_kb,
    attach_proxy_kb,
    cancel_kb,
    main_menu_kb,
    proxies_list_kb,
    settings_kb,
)
from app.bot.states import AddAccountStates, AddProxyStates, SettingsStates
from app.config import AppConfig
from app.db.models import AccountStatus
from app.db.repository import SupabaseRepository
from app.security import SessionCipher
from app.services.github_trigger import trigger_github_worker
from app.services.proxy_checker import check_proxy_connectivity, parse_proxy_url
from app.userbot.client_factory import create_telegram_client

router = Router(name="admin_panel")

UZ_TZ = timezone(timedelta(hours=5))


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    return dt.astimezone(UZ_TZ).strftime("%Y-%m-%d %H:%M (UZT)")


# ============================================================================
# ASOSIY MENYU VA BEKOR QILISH
# ============================================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 <b>X Obunachi Multi-Userbot Admin Paneli</b>\n\n"
        "Bu yerda barcha Telegram userbot akkauntlarini qo‘shishingiz, "
        "proksi biriktirishingiz, sozlamalarni o‘zgartirishingiz va "
        "kunlik statistikani kuzatishingiz mumkin.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "fsm:cancel")
async def cb_cancel_fsm(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("❌ Amal bekor qilindi.")
    await call.answer()


# ============================================================================
# AKKAUNTLAR RO'YXATI VA BOSHQARUVI
# ============================================================================
async def _render_accounts_text(repo: SupabaseRepository) -> str:
    accounts = await repo.list_accounts()
    if not accounts:
        return (
            "📱 <b>Akkauntlar ro‘yxati bo‘sh.</b>\n\n"
            "Yangi userbot qo‘shish uchun <b>➕ Akkaunt qo‘shish</b> tugmasini bosing."
        )
    active = sum(1 for a in accounts if a.status == AccountStatus.ACTIVE)
    total_bal = sum(a.balance for a in accounts)
    return (
        f"📱 <b>Ulangan akkauntlar: {len(accounts)} ta</b> (🟢 Faol: {active} ta)\n"
        f"💰 Jami balans: <b>{total_bal:.2f} ₽</b>\n\n"
        "Boshqarish uchun kerakli akkauntni tanlang:"
    )


@router.message(F.text == "📱 Akkauntlar")
async def msg_accounts(message: Message, repo: SupabaseRepository) -> None:
    accounts = await repo.list_accounts()
    text = await _render_accounts_text(repo)
    await message.answer(text, reply_markup=accounts_list_kb(accounts))


@router.callback_query(F.data == "acc:list")
async def cb_accounts_list(call: CallbackQuery, repo: SupabaseRepository) -> None:
    accounts = await repo.list_accounts()
    text = await _render_accounts_text(repo)
    await call.message.edit_text(text, reply_markup=accounts_list_kb(accounts))
    await call.answer()


@router.callback_query(F.data.startswith("acc:view:"))
async def cb_account_view(call: CallbackQuery, repo: SupabaseRepository) -> None:
    acc_id = int(call.data.split(":")[-1])
    acc = await repo.get_account(acc_id)
    if not acc:
        await call.answer("Akkaunt topilmadi!", show_alert=True)
        return

    proxy_label = "Yo‘q (to‘g‘ridan-to‘g‘ri)"
    if acc.proxy_id:
        p = await repo.get_proxy(acc.proxy_id)
        if p:
            proxy_label = f"#{p.id} {p.masked_url()}"

    active_channels = await repo.count_active_subscriptions(acc.id)
    reason_line = f"\n📝 Izoh: <code>{acc.status_reason}</code>" if acc.status_reason else ""

    text = (
        f"📱 <b>Akkaunt #{acc.id}: {acc.phone}</b>\n"
        f"• Holat: <b>{acc.status_emoji}</b>{reason_line}\n"
        f"• Balans: <b>{acc.balance:.2f} ₽</b>\n"
        f"• Proksi: <code>{proxy_label}</code>\n"
        f"• Faol obunalar: <b>{active_channels} / 500</b>\n"
        f"• Oxirgi bonus: {_fmt_dt(acc.last_bonus_at)}\n"
        f"• Oxirgi faollik: {_fmt_dt(acc.last_active_at)}\n"
        f"• Keyingi sikl: {_fmt_dt(acc.next_run_at)}"
    )
    await call.message.edit_text(text, reply_markup=account_detail_kb(acc))
    await call.answer()


@router.callback_query(F.data.startswith("acc:start:"))
async def cb_account_start(call: CallbackQuery, repo: SupabaseRepository) -> None:
    acc_id = int(call.data.split(":")[-1])
    await repo.update_account(
        acc_id,
        {
            "status": AccountStatus.ACTIVE,
            "status_reason": None,
            "flood_until": None,
            "next_run_at": None,
        },
    )
    await call.answer("🟢 Akkaunt ishga tushirildi!")
    await cb_account_view(call, repo)


@router.callback_query(F.data.startswith("acc:stop:"))
async def cb_account_stop(call: CallbackQuery, repo: SupabaseRepository) -> None:
    acc_id = int(call.data.split(":")[-1])
    await repo.update_account(acc_id, {"status": AccountStatus.PAUSED})
    await call.answer("🔴 Akkaunt to‘xtatildi!")
    await cb_account_view(call, repo)


@router.callback_query(F.data == "acc:start_all")
async def cb_accounts_start_all(call: CallbackQuery, repo: SupabaseRepository) -> None:
    count = await repo.set_all_accounts_status(AccountStatus.ACTIVE)
    await call.answer(f"🟢 {count} ta akkaunt faollashtirildi!", show_alert=True)
    await cb_accounts_list(call, repo)


@router.callback_query(F.data == "acc:stop_all")
async def cb_accounts_stop_all(call: CallbackQuery, repo: SupabaseRepository) -> None:
    count = await repo.set_all_accounts_status(AccountStatus.PAUSED)
    await call.answer(f"🔴 {count} ta akkaunt to‘xtatildi!", show_alert=True)
    await cb_accounts_list(call, repo)


@router.callback_query(F.data.startswith("acc:delete:"))
async def cb_account_delete(call: CallbackQuery, repo: SupabaseRepository) -> None:
    acc_id = int(call.data.split(":")[-1])
    await repo.delete_account(acc_id)
    await call.answer("🗑 Akkaunt bazadan o‘chirildi.", show_alert=True)
    await cb_accounts_list(call, repo)


@router.callback_query(F.data.startswith("acc:proxy_menu:"))
async def cb_account_proxy_menu(call: CallbackQuery, repo: SupabaseRepository) -> None:
    acc_id = int(call.data.split(":")[-1])
    proxies = await repo.list_proxies()
    await call.message.edit_text(
        f"🌐 <b>Akkaunt #{acc_id} uchun proksi tanlang:</b>",
        reply_markup=attach_proxy_kb(acc_id, proxies),
    )
    await call.answer()


@router.callback_query(F.data.startswith("acc:set_proxy:"))
async def cb_account_set_proxy(call: CallbackQuery, repo: SupabaseRepository) -> None:
    parts = call.data.split(":")
    acc_id = int(parts[2])
    proxy_id = int(parts[3])
    if proxy_id == 0:
        await repo.assign_proxy(acc_id, None)
        await call.answer("✅ Proksi olib tashlandi.")
    else:
        proxy = await repo.get_proxy(proxy_id)
        if not proxy:
            await call.answer("Proksi topilmadi!", show_alert=True)
            return
        ok, msg = await check_proxy_connectivity(proxy)
        if not ok:
            await call.answer(f"❌ {msg}", show_alert=True)
            return
        await repo.assign_proxy(acc_id, proxy_id)
        await call.answer("✅ Proksi tekshirildi va biriktirildi!")
    call.data = f"acc:view:{acc_id}"
    await cb_account_view(call, repo)


# ============================================================================
# YANGI AKKAUNT QO'SHISH (INTERAKTIV FSM + TELETHON STRINGSESSION)
# ============================================================================
@router.message(F.text == "➕ Akkaunt qo‘shish")
async def msg_add_account(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccountStates.waiting_phone)
    await message.answer(
        "📱 <b>Yangi Telegram akkaunt qo‘shish</b>\n\n"
        "Telefon raqamni xalqaro formatda kiriting (masalan: <code>+998901234567</code>):",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data == "acc:add")
async def cb_add_account(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccountStates.waiting_phone)
    await call.message.edit_text(
        "📱 <b>Yangi Telegram akkaunt qo‘shish</b>\n\n"
        "Telefon raqamni xalqaro formatda kiriting (masalan: <code>+998901234567</code>):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AddAccountStates.waiting_phone)
async def fsm_receive_phone(
    message: Message,
    state: FSMContext,
    config: AppConfig,
) -> None:
    phone = re.sub(r"[^\d+]", "", (message.text or "").strip())
    if not phone.startswith("+") or len(phone) < 9:
        await message.answer(
            "❌ Telefon raqam noto‘g‘ri. Namuna: <code>+998901234567</code>",
            reply_markup=cancel_kb(),
        )
        return

    client = create_telegram_client(
        session_string="",
        api_id=config.default_api_id,
        api_hash=config.default_api_hash,
    )
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        temp_session = client.session.save()
        await state.update_data(
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            temp_session=temp_session,
        )
        await state.set_state(AddAccountStates.waiting_code)
        await message.answer(
            "📩 Telegram orqali 5 xonali tasdiqlash kodi yuborildi.\n\n"
            "Kodni <b>bo‘sh joy bilan</b> yoki oddiy kiriting (masalan: <code>1 2 3 4 5</code>):",
            reply_markup=cancel_kb(),
        )
    except PhoneNumberInvalidError:
        await message.answer("❌ Bu telefon raqami Telegramda mavjud emas.", reply_markup=cancel_kb())
    except FloodWaitError as exc:
        await message.answer(
            f"⚠️ Telegram cheklovi (FloodWait): {exc.seconds} soniyadan keyin urinib ko‘ring."
        )
        await state.clear()
    except Exception as exc:
        logger.exception("send_code_request xatosi: {}", exc)
        await message.answer(f"❌ Kod yuborishda xatolik: <code>{exc}</code>")
        await state.clear()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@router.message(AddAccountStates.waiting_code)
async def fsm_receive_code(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    repo: SupabaseRepository,
    cipher: SessionCipher,
) -> None:
    code = re.sub(r"\D", "", (message.text or "").strip())
    if len(code) < 4:
        await message.answer("❌ Kod faqat raqamlardan iborat bo‘lishi kerak.", reply_markup=cancel_kb())
        return

    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    temp_session = data["temp_session"]

    client = create_telegram_client(
        session_string=temp_session,
        api_id=config.default_api_id,
        api_hash=config.default_api_hash,
    )
    try:
        await client.connect()
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        final_session = client.session.save()
        await _save_authorized_account(
            message=message,
            state=state,
            config=config,
            repo=repo,
            cipher=cipher,
            phone=phone,
            raw_session=final_session,
        )
    except SessionPasswordNeededError:
        updated_temp = client.session.save()
        await state.update_data(temp_session=updated_temp)
        await state.set_state(AddAccountStates.waiting_2fa)
        await message.answer(
            "🔐 Ushbu akkauntda <b>2FA (ikki bosqichli parol)</b> yoqilgan.\n"
            "Iltimos, bulutli parolni kiriting:",
            reply_markup=cancel_kb(),
        )
    except PhoneCodeInvalidError:
        await message.answer("❌ Tasdiqlash kodi noto‘g‘ri. Qaytadan kiriting:", reply_markup=cancel_kb())
    except PhoneCodeExpiredError:
        await message.answer("❌ Kod muddati tugagan. Akkaunt qo‘shishni qaytadan boshlang.")
        await state.clear()
    except Exception as exc:
        logger.exception("sign_in xatosi: {}", exc)
        await message.answer(f"❌ Kirishda xatolik: <code>{exc}</code>")
        await state.clear()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@router.message(AddAccountStates.waiting_2fa)
async def fsm_receive_2fa(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    repo: SupabaseRepository,
    cipher: SessionCipher,
) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    phone = data["phone"]
    temp_session = data["temp_session"]

    client = create_telegram_client(
        session_string=temp_session,
        api_id=config.default_api_id,
        api_hash=config.default_api_hash,
    )
    try:
        await client.connect()
        await client.sign_in(password=password)
        final_session = client.session.save()
        await _save_authorized_account(
            message=message,
            state=state,
            config=config,
            repo=repo,
            cipher=cipher,
            phone=phone,
            raw_session=final_session,
        )
    except PasswordHashInvalidError:
        await message.answer("❌ 2FA paroli noto‘g‘ri. Qaytadan urinib ko‘ring:", reply_markup=cancel_kb())
    except Exception as exc:
        logger.exception("2FA sign_in xatosi: {}", exc)
        await message.answer(f"❌ 2FA bilan kirishda xatolik: <code>{exc}</code>")
        await state.clear()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _save_authorized_account(
    *,
    message: Message,
    state: FSMContext,
    config: AppConfig,
    repo: SupabaseRepository,
    cipher: SessionCipher,
    phone: str,
    raw_session: str,
) -> None:
    session_name = f"acc_{re.sub(r'[^0-9]', '', phone)}"
    stored_session = cipher.encrypt(raw_session)
    acc = await repo.upsert_account(
        phone=phone,
        session_name=session_name,
        session_string=stored_session,
        api_id=config.default_api_id,
        api_hash=config.default_api_hash,
        status=AccountStatus.ACTIVE,
    )
    await state.clear()
    await message.answer(
        f"✅ <b>Akkaunt muvaffaqiyatli ulandi va faollashtirildi!</b>\n\n"
        f"• ID: <b>#{acc.id}</b>\n"
        f"• Telefon: <code>{acc.phone}</code>\n"
        f"• Sessiya: Supabase bazasida {'shifrlangan' if cipher.enabled else 'saqlangan'}\n"
        f"• Holat: <b>{acc.status_emoji}</b>",
        reply_markup=main_menu_kb(),
    )


# ============================================================================
# PROKSILAR BOSHQARUVI
# ============================================================================
@router.message(F.text == "🌐 Proksilar")
async def msg_proxies(message: Message, repo: SupabaseRepository) -> None:
    proxies = await repo.list_proxies()
    text = (
        f"🌐 <b>Proksilar ro‘yxati ({len(proxies)} ta)</b>\n\n"
        "O‘chirish uchun proksini bosing yoki yangisini qo‘shing:"
        if proxies
        else "🌐 <b>Proksilar ro‘yxati bo‘sh.</b>\nYangi proksi qo‘shishingiz mumkin:"
    )
    await message.answer(text, reply_markup=proxies_list_kb(proxies))


@router.callback_query(F.data == "proxy:add")
async def cb_proxy_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddProxyStates.waiting_proxy_url)
    await call.message.edit_text(
        "🌐 <b>Yangi proksi qo‘shish</b>\n\n"
        "Proksini quyidagi formatda yuboring:\n"
        "<code>socks5://user:pass@ip:port</code>\n"
        "yoki <code>http://ip:port</code>",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AddProxyStates.waiting_proxy_url)
async def fsm_receive_proxy(
    message: Message, state: FSMContext, repo: SupabaseRepository
) -> None:
    raw = (message.text or "").strip()
    try:
        parsed = parse_proxy_url(raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}", reply_markup=cancel_kb())
        return

    status_msg = await message.answer("⏳ Proksi Telegram serveriga ulanishi tekshirilmoqda...")
    ok, details = await check_proxy_connectivity(parsed)
    if not ok:
        await status_msg.edit_text(f"❌ {details}\n\nBoshqa proksi yuboring:", reply_markup=cancel_kb())
        return

    saved = await repo.add_proxy(
        protocol=parsed.protocol,
        host=parsed.host,
        port=parsed.port,
        username=parsed.username,
        password=parsed.password,
        is_active=True,
    )
    await state.clear()
    await status_msg.edit_text(
        f"✅ <b>Proksi tekshirildi va saqlandi!</b>\n"
        f"• ID: <b>#{saved.id}</b>\n"
        f"• Manzil: <code>{saved.masked_url()}</code>"
    )


@router.callback_query(F.data.startswith("proxy:delete:"))
async def cb_proxy_delete(call: CallbackQuery, repo: SupabaseRepository) -> None:
    proxy_id = int(call.data.split(":")[-1])
    await repo.delete_proxy(proxy_id)
    proxies = await repo.list_proxies()
    await call.message.edit_text(
        f"🌐 <b>Proksilar ro‘yxati ({len(proxies)} ta)</b>",
        reply_markup=proxies_list_kb(proxies),
    )
    await call.answer("🗑 Proksi o‘chirildi.")


# ============================================================================
# DINAMIK SOZLAMALAR
# ============================================================================
@router.message(F.text == "⚙️ Sozlamalar")
async def msg_settings(message: Message, repo: SupabaseRepository, config: AppConfig) -> None:
    s = await repo.get_settings(default_target_bot=config.default_target_bot)
    text = (
        "⚙️ <b>Tizim sozlamalari (Dinamik)</b>\n\n"
        f"• Maqsadli bot: <code>@{s.target_bot_username}</code>\n"
        f"• Sikl oralig‘i: <b>{s.cycle_min_minutes}–{s.cycle_max_minutes} daqiqa</b>\n"
        f"• Harakatlar pauzasi: <b>{s.action_delay_min_sec:.0f}–{s.action_delay_max_sec:.0f} soniya</b>\n"
        f"• Auto-Leave muddati: <b>{s.auto_leave_days} kun</b> (15 kunlik jarimadan himoya)\n"
        f"• Bir sikldagi maks. vazifa: <b>{s.max_tasks_per_cycle} ta</b>"
    )
    await message.answer(text, reply_markup=settings_kb())


@router.callback_query(F.data == "set:cycle_range")
async def cb_set_cycle(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_cycle_range)
    await call.message.edit_text(
        "⏱ Sikl oralig‘ini <code>min-max</code> daqiqada kiriting (masalan: <code>20-25</code>):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(SettingsStates.waiting_cycle_range)
async def fsm_set_cycle(message: Message, state: FSMContext, repo: SupabaseRepository) -> None:
    m = re.match(r"^\s*(\d+)\s*[-– ]\s*(\d+)\s*$", message.text or "")
    if not m or int(m.group(1)) < 1 or int(m.group(2)) < int(m.group(1)):
        await message.answer("❌ Format noto‘g‘ri. Namuna: <code>20-25</code>", reply_markup=cancel_kb())
        return
    await repo.set_setting("cycle_min_minutes", m.group(1))
    await repo.set_setting("cycle_max_minutes", m.group(2))
    await state.clear()
    await message.answer(f"✅ Sikl oralig‘i <b>{m.group(1)}–{m.group(2)} daqiqa</b> qilib saqlandi.")


@router.callback_query(F.data == "set:delay_range")
async def cb_set_delay(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_delay_range)
    await call.message.edit_text(
        "⏳ Harakatlar orasidagi pauzani <code>min-max</code> soniyada kiriting (masalan: <code>3-8</code>):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(SettingsStates.waiting_delay_range)
async def fsm_set_delay(message: Message, state: FSMContext, repo: SupabaseRepository) -> None:
    m = re.match(r"^\s*(\d+)\s*[-– ]\s*(\d+)\s*$", message.text or "")
    if not m or int(m.group(1)) < 1 or int(m.group(2)) < int(m.group(1)):
        await message.answer("❌ Format noto‘g‘ri. Namuna: <code>3-8</code>", reply_markup=cancel_kb())
        return
    await repo.set_setting("action_delay_min_sec", m.group(1))
    await repo.set_setting("action_delay_max_sec", m.group(2))
    await state.clear()
    await message.answer(f"✅ Harakat pauzasi <b>{m.group(1)}–{m.group(2)} soniya</b> qilib saqlandi.")


@router.callback_query(F.data == "set:target_bot")
async def cb_set_target_bot(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_target_bot)
    await call.message.edit_text(
        "🤖 Maqsadli bot username’ini kiriting (masalan: <code>@XObunachiBot</code>):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(SettingsStates.waiting_target_bot)
async def fsm_set_target_bot(message: Message, state: FSMContext, repo: SupabaseRepository) -> None:
    uname = (message.text or "").strip().lstrip("@")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", uname):
        await message.answer("❌ Noto‘g‘ri username. Namuna: <code>@XObunachiBot</code>", reply_markup=cancel_kb())
        return
    await repo.set_setting("target_bot_username", uname)
    await state.clear()
    await message.answer(f"✅ Maqsadli bot <code>@{uname}</code> qilib o‘zgartirildi.")


@router.callback_query(F.data == "set:leave_days")
async def cb_set_leave_days(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_leave_days)
    await call.message.edit_text(
        "🚪 Kanallardan avtomatik chiqish muddatini (kamida 15 kun) kiriting (tavsiya: <code>16</code>):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(SettingsStates.waiting_leave_days)
async def fsm_set_leave_days(message: Message, state: FSMContext, repo: SupabaseRepository) -> None:
    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 15:
        await message.answer(
            "❌ Jarima tushmasligi uchun kamida <b>15</b> kun bo‘lishi kerak (tavsiya: 16).",
            reply_markup=cancel_kb(),
        )
        return
    await repo.set_setting("auto_leave_days", val)
    await state.clear()
    await message.answer(f"✅ Auto-Leave muddati <b>{val} kun</b> qilib saqlandi.")


# ============================================================================
# STATISTIKA VA QO'LDA WORKER ISHGA TUSHIRISH
# ============================================================================
@router.message(F.text == "📊 Statistika")
async def msg_stats(message: Message, repo: SupabaseRepository) -> None:
    st = await repo.get_global_stats()
    lines = [
        "📊 <b>Umumiy va Kunlik Statistika</b>\n",
        f"• Jami akkauntlar: <b>{st.total_accounts} ta</b> (🟢 Faol: {st.active_accounts})",
        f"• Jami balans (barcha akkauntlar): <b>{st.total_balance:.2f} ₽</b>",
        f"• Bugungi obunalar: <b>{st.today_subs} ta</b> | Bugungi daromad: <b>+{st.today_earned:.2f} ₽</b>",
        f"• Umumiy obunalar tarixi: <b>{st.total_subs_all_time} ta</b>\n",
        "<b>Akkauntlar kesimida:</b>",
    ]
    if not st.per_account:
        lines.append("— Hali akkaunt qo‘shilmagan.")
    else:
        for item in st.per_account:
            icon = {
                AccountStatus.ACTIVE: "🟢",
                AccountStatus.PAUSED: "🔴",
                AccountStatus.FLOOD_WAIT: "⚠️",
                AccountStatus.BANNED: "❌",
            }.get(item.status, "⚪")
            lines.append(
                f"{icon} <code>{item.phone}</code> | Balans: <b>{item.balance:.2f} ₽</b> | "
                f"Bugun: {item.today_subs} ta (+{item.today_earned:.2f} ₽) | "
                f"Kanallar: {item.active_channels}/500"
            )
    await message.answer("\n".join(lines))


@router.message(F.text == "🚀 Hozir ishga tushirish")
async def msg_trigger_worker(message: Message, config: AppConfig) -> None:
    ok, info = await trigger_github_worker(
        github_token=config.github_token,
        github_repo=config.github_repo,
        workflow_file=config.github_worker_workflow,
    )
    prefix = "🚀" if ok else "ℹ️"
    await message.answer(f"{prefix} {info}")
