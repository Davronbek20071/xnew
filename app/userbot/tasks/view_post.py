from __future__ import annotations

from loguru import logger
from telethon import TelegramClient

from app.db.models import AccountModel
from app.userbot.tasks.base import BaseTaskHandler, TaskRunResult


class ViewPostTaskHandler(BaseTaskHandler):
    """Kelajakda 'Post ko'rish' ('Shu yerda ko'rish') vazifasini qo'shish uchun tayyor modul.

    Modulli arxitektura tufayli ushbu klassni `AutomationWorker.task_handlers` ro'yxatiga
    qo'shish orqali yangi vazifa turini asosiy kodni buzmasdan yoqish mumkin.
    """

    name = "view_post"

    async def execute(
        self,
        client: TelegramClient,
        account: AccountModel,
        bot_entity: object,
    ) -> TaskRunResult:
        logger.debug("[{}] ViewPostTaskHandler hali faollashtirilmagan.", account.phone)
        return TaskRunResult(stopped_reason="not_enabled")
