from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class AccountStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    FLOOD_WAIT = "flood_wait"
    BANNED = "banned"


class ProxyModel(BaseModel):
    id: int
    protocol: str = "socks5"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    is_active: bool = True
    last_checked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    def masked_url(self) -> str:
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.protocol}://{auth}{self.host}:{self.port}"

    def full_url(self) -> str:
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        if self.username:
            return f"{self.protocol}://{self.username}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"


class AccountModel(BaseModel):
    id: int
    phone: str
    session_name: str
    session_string: str
    api_id: int
    api_hash: str
    proxy_id: Optional[int] = None
    status: AccountStatus = AccountStatus.PAUSED
    status_reason: Optional[str] = None
    balance: Decimal = Decimal("0.00")
    last_bonus_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    flood_until: Optional[datetime] = None
    last_active_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def status_emoji(self) -> str:
        mapping = {
            AccountStatus.ACTIVE: "🟢 Ishlamoqda",
            AccountStatus.PAUSED: "🔴 To‘xtatilgan",
            AccountStatus.FLOOD_WAIT: "⚠️ FloodWait",
            AccountStatus.BANNED: "❌ Bloklangan",
        }
        return mapping.get(self.status, "⚪ Noma'lum")


class SubscriptionModel(BaseModel):
    id: int
    account_id: int
    chat_id: Optional[int] = None
    username_or_link: str
    title: Optional[str] = None
    is_mandatory: bool = False
    joined_at: datetime
    leave_after: datetime
    is_left: bool = False
    left_at: Optional[datetime] = None


class StatsLogModel(BaseModel):
    id: int
    account_id: Optional[int] = None
    action_type: str
    amount: Decimal = Decimal("0.00")
    details: Optional[str] = None
    created_at: datetime


class DynamicSettings(BaseModel):
    """Admin paneldan boshqariladigan dinamik sozlamalar."""

    target_bot_username: str = "XObunachiBot"
    cycle_min_minutes: int = 20
    cycle_max_minutes: int = 25
    action_delay_min_sec: float = 3.0
    action_delay_max_sec: float = 8.0
    auto_leave_days: int = 16
    max_tasks_per_cycle: int = 25

    @classmethod
    def from_dict(cls, raw: Dict[str, str], default_target_bot: str = "XObunachiBot") -> "DynamicSettings":
        def _int(key: str, default: int) -> int:
            try:
                return int(float(raw.get(key, default)))
            except (TypeError, ValueError):
                return default

        def _float(key: str, default: float) -> float:
            try:
                return float(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        c_min = max(1, _int("cycle_min_minutes", 20))
        c_max = max(c_min, _int("cycle_max_minutes", 25))
        d_min = max(1.0, _float("action_delay_min_sec", 3.0))
        d_max = max(d_min, _float("action_delay_max_sec", 8.0))
        target = (raw.get("target_bot_username") or default_target_bot).strip().lstrip("@")

        return cls(
            target_bot_username=target or default_target_bot,
            cycle_min_minutes=c_min,
            cycle_max_minutes=c_max,
            action_delay_min_sec=d_min,
            action_delay_max_sec=d_max,
            auto_leave_days=max(15, _int("auto_leave_days", 16)),
            max_tasks_per_cycle=max(1, _int("max_tasks_per_cycle", 25)),
        )


class AccountStatsSummary(BaseModel):
    account_id: int
    phone: str
    status: AccountStatus
    balance: Decimal
    total_subs: int = 0
    today_subs: int = 0
    today_earned: Decimal = Decimal("0.00")
    active_channels: int = 0


class GlobalStatsSummary(BaseModel):
    total_accounts: int = 0
    active_accounts: int = 0
    total_balance: Decimal = Decimal("0.00")
    total_subs_all_time: int = 0
    today_subs: int = 0
    today_earned: Decimal = Decimal("0.00")
    per_account: list[AccountStatsSummary] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)
