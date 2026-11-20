from __future__ import annotations

from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.models import AccountModel, AccountStatus, ProxyModel


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📱 Akkauntlar"),
                KeyboardButton(text="➕ Akkaunt qo‘shish"),
            ],
            [
                KeyboardButton(text="🌐 Proksilar"),
                KeyboardButton(text="⚙️ Sozlamalar"),
            ],
            [
                KeyboardButton(text="📊 Statistika"),
                KeyboardButton(text="🚀 Hozir ishga tushirish"),
            ],
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="fsm:cancel")]
        ]
    )


def accounts_list_kb(accounts: List[AccountModel]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for acc in accounts:
        icon = {
            AccountStatus.ACTIVE: "🟢",
            AccountStatus.PAUSED: "🔴",
            AccountStatus.FLOOD_WAIT: "⚠️",
            AccountStatus.BANNED: "❌",
        }.get(acc.status, "⚪")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {acc.phone} | {acc.balance:.2f} ₽",
                    callback_data=f"acc:view:{acc.id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="▶️ Barchasini yoqish", callback_data="acc:start_all"),
            InlineKeyboardButton(text="⏸ Barchasini to‘xtatish", callback_data="acc:stop_all"),
        ]
    )
    rows.append([InlineKeyboardButton(text="➕ Yangi akkaunt qo‘shish", callback_data="acc:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_detail_kb(acc: AccountModel) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="⏸ To‘xtatish", callback_data=f"acc:stop:{acc.id}")
        if acc.status in (AccountStatus.ACTIVE, AccountStatus.FLOOD_WAIT)
        else InlineKeyboardButton(text="▶️ Ishga tushirish", callback_data=f"acc:start:{acc.id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle_btn],
            [
                InlineKeyboardButton(
                    text="🌐 Proksi biriktirish", callback_data=f"acc:proxy_menu:{acc.id}"
                )
            ],
            [InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"acc:delete:{acc.id}")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="acc:list")],
        ]
    )


def attach_proxy_kb(account_id: int, proxies: List[ProxyModel]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🚫 Proksisiz (To‘g‘ridan-to‘g‘ri)",
                callback_data=f"acc:set_proxy:{account_id}:0",
            )
        ]
    ]
    for p in proxies:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🌐 #{p.id} {p.masked_url()}",
                    callback_data=f"acc:set_proxy:{account_id}:{p.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"acc:view:{account_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def proxies_list_kb(proxies: List[ProxyModel]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for p in proxies:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 #{p.id} {p.masked_url()}",
                    callback_data=f"proxy:delete:{p.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Proksi qo‘shish", callback_data="proxy:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱ Sikl oralig‘i (daq)", callback_data="set:cycle_range"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏳ Harakat pauzasi (son)", callback_data="set:delay_range"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Maqsadli bot username", callback_data="set:target_bot"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚪 Auto-Leave muddati (kun)", callback_data="set:leave_days"
                )
            ],
        ]
    )
