from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddAccountStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()


class AddProxyStates(StatesGroup):
    waiting_proxy_url = State()


class SettingsStates(StatesGroup):
    waiting_cycle_range = State()
    waiting_delay_range = State()
    waiting_target_bot = State()
    waiting_leave_days = State()
