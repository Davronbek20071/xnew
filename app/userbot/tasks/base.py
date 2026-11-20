from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from telethon import TelegramClient

from app.db.models import AccountModel, DynamicSettings
from app.db.repository import SupabaseRepository
from app.services.notifier import AdminNotifier


@dataclass
class TaskRunResult:
    completed_count: int = 0
    earned_amount: Decimal = Decimal("0.00")
    channels_limit_reached: bool = False
    stopped_reason: str = "completed"


class BaseTaskHandler(ABC):
    """Yangi vazifa turlarini (masalan, Post ko'rish) oson ulash uchun bazaviy klass."""

    name: str = "base"

    def __init__(
        self,
        *,
        repo: SupabaseRepository,
        notifier: AdminNotifier,
        settings: DynamicSettings,
    ) -> None:
        self.repo = repo
        self.notifier = notifier
        self.settings = settings

    @abstractmethod
    async def execute(
        self,
        client: TelegramClient,
        account: AccountModel,
        bot_entity: object,
    ) -> TaskRunResult:
        """Vazifa siklini bajaradi."""
