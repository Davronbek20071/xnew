from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.db.models import (
    AccountModel,
    AccountStatsSummary,
    AccountStatus,
    DynamicSettings,
    GlobalStatsSummary,
    ProxyModel,
    SubscriptionModel,
)


class SupabaseError(RuntimeError):
    """Supabase REST API bilan ishlashda yuzaga kelgan xatolik."""


class SupabaseRepository:
    """Supabase PostgREST ustida qurilgan yengil va to'liq asinxron repository.

    Vercel serverless muhitida ulanishlar puli (connection pool) to'lib ketishining
    oldini olish va GitHub Actions/VPS bilan bir xil ishlashi uchun HTTPX REST API ishlatiladi.
    """

    def __init__(self, supabase_url: str, service_role_key: str, timeout: float = 20.0) -> None:
        self.base_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        table: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        prefer: Optional[str] = None,
    ) -> Any:
        extra_headers: Dict[str, str] = {}
        if prefer:
            extra_headers["Prefer"] = prefer

        resp = await self._client.request(
            method=method,
            url=f"/{table}",
            params=params,
            json=json_body,
            headers=extra_headers,
        )
        if resp.status_code >= 400:
            logger.error(
                "Supabase xatoligi: {} /{} -> {} {}",
                method,
                table,
                resp.status_code,
                resp.text[:500],
            )
            raise SupabaseError(f"Supabase {resp.status_code}: {resp.text[:300]}")
        if not resp.text:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # ACCOUNTS
    # ------------------------------------------------------------------
    async def list_accounts(self) -> List[AccountModel]:
        rows = await self._request("GET", "accounts", params={"select": "*", "order": "id.asc"})
        return [AccountModel.model_validate(r) for r in (rows or [])]

    async def list_active_accounts(self) -> List[AccountModel]:
        rows = await self._request(
            "GET",
            "accounts",
            params={"select": "*", "status": "in.(active,flood_wait)", "order": "id.asc"},
        )
        return [AccountModel.model_validate(r) for r in (rows or [])]

    async def get_account(self, account_id: int) -> Optional[AccountModel]:
        rows = await self._request(
            "GET", "accounts", params={"select": "*", "id": f"eq.{account_id}", "limit": 1}
        )
        return AccountModel.model_validate(rows[0]) if rows else None

    async def get_account_by_phone(self, phone: str) -> Optional[AccountModel]:
        rows = await self._request(
            "GET", "accounts", params={"select": "*", "phone": f"eq.{phone}", "limit": 1}
        )
        return AccountModel.model_validate(rows[0]) if rows else None

    async def upsert_account(
        self,
        *,
        phone: str,
        session_name: str,
        session_string: str,
        api_id: int,
        api_hash: str,
        proxy_id: Optional[int] = None,
        status: AccountStatus = AccountStatus.ACTIVE,
    ) -> AccountModel:
        payload = {
            "phone": phone,
            "session_name": session_name,
            "session_string": session_string,
            "api_id": api_id,
            "api_hash": api_hash,
            "proxy_id": proxy_id,
            "status": status.value,
            "status_reason": None,
            "flood_until": None,
        }
        rows = await self._request(
            "POST",
            "accounts",
            params={"on_conflict": "phone"},
            json_body=payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
        return AccountModel.model_validate(rows[0])

    async def update_account(self, account_id: int, fields: Dict[str, Any]) -> Optional[AccountModel]:
        clean_fields: Dict[str, Any] = {}
        for k, v in fields.items():
            if isinstance(v, datetime):
                clean_fields[k] = v.astimezone(timezone.utc).isoformat()
            elif isinstance(v, Decimal):
                clean_fields[k] = float(v)
            elif isinstance(v, AccountStatus):
                clean_fields[k] = v.value
            else:
                clean_fields[k] = v

        rows = await self._request(
            "PATCH",
            "accounts",
            params={"id": f"eq.{account_id}"},
            json_body=clean_fields,
            prefer="return=representation",
        )
        return AccountModel.model_validate(rows[0]) if rows else None

    async def set_all_accounts_status(self, target_status: AccountStatus) -> int:
        """Faqat ban bo'lmagan akkauntlarni ommaviy yoqish yoki to'xtatish."""
        rows = await self._request(
            "PATCH",
            "accounts",
            params={"status": "neq.banned"},
            json_body={
                "status": target_status.value,
                "status_reason": None,
                "flood_until": None,
            },
            prefer="return=representation",
        )
        return len(rows or [])

    async def delete_account(self, account_id: int) -> None:
        await self._request("DELETE", "accounts", params={"id": f"eq.{account_id}"})

    # ------------------------------------------------------------------
    # PROXIES
    # ------------------------------------------------------------------
    async def list_proxies(self) -> List[ProxyModel]:
        rows = await self._request("GET", "proxies", params={"select": "*", "order": "id.asc"})
        return [ProxyModel.model_validate(r) for r in (rows or [])]

    async def get_proxy(self, proxy_id: int) -> Optional[ProxyModel]:
        rows = await self._request(
            "GET", "proxies", params={"select": "*", "id": f"eq.{proxy_id}", "limit": 1}
        )
        return ProxyModel.model_validate(rows[0]) if rows else None

    async def add_proxy(
        self,
        *,
        protocol: str,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        is_active: bool = True,
    ) -> ProxyModel:
        payload = {
            "protocol": protocol.lower(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "is_active": is_active,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }
        rows = await self._request(
            "POST",
            "proxies",
            json_body=payload,
            prefer="return=representation",
        )
        return ProxyModel.model_validate(rows[0])

    async def delete_proxy(self, proxy_id: int) -> None:
        await self._request("DELETE", "proxies", params={"id": f"eq.{proxy_id}"})

    async def assign_proxy(self, account_id: int, proxy_id: Optional[int]) -> Optional[AccountModel]:
        return await self.update_account(account_id, {"proxy_id": proxy_id})

    # ------------------------------------------------------------------
    # SUBSCRIPTIONS (16-DAY AUTO-LEAVE TRACKING)
    # ------------------------------------------------------------------
    async def add_subscription(
        self,
        *,
        account_id: int,
        chat_id: Optional[int],
        username_or_link: str,
        title: Optional[str] = None,
        is_mandatory: bool = False,
        leave_days: int = 16,
    ) -> SubscriptionModel:
        now = datetime.now(timezone.utc)
        leave_after = now + timedelta(days=leave_days)
        payload = {
            "account_id": account_id,
            "chat_id": chat_id,
            "username_or_link": username_or_link,
            "title": title,
            "is_mandatory": is_mandatory,
            "joined_at": now.isoformat(),
            "leave_after": leave_after.isoformat(),
            "is_left": False,
        }
        rows = await self._request(
            "POST",
            "subscriptions",
            json_body=payload,
            prefer="return=representation",
        )
        return SubscriptionModel.model_validate(rows[0])

    async def list_due_subscriptions(
        self, account_id: Optional[int] = None, limit: int = 50
    ) -> List[SubscriptionModel]:
        now_iso = datetime.now(timezone.utc).isoformat()
        params: Dict[str, Any] = {
            "select": "*",
            "is_left": "eq.false",
            "leave_after": f"lte.{now_iso}",
            "order": "leave_after.asc",
            "limit": limit,
        }
        if account_id is not None:
            params["account_id"] = f"eq.{account_id}"
        rows = await self._request("GET", "subscriptions", params=params)
        return [SubscriptionModel.model_validate(r) for r in (rows or [])]

    async def mark_subscription_left(self, sub_id: int) -> None:
        await self._request(
            "PATCH",
            "subscriptions",
            params={"id": f"eq.{sub_id}"},
            json_body={
                "is_left": True,
                "left_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def count_active_subscriptions(self, account_id: int) -> int:
        rows = await self._request(
            "GET",
            "subscriptions",
            params={
                "select": "id",
                "account_id": f"eq.{account_id}",
                "is_left": "eq.false",
            },
        )
        return len(rows or [])

    # ------------------------------------------------------------------
    # STATS & LOGS
    # ------------------------------------------------------------------
    async def log_stat(
        self,
        *,
        account_id: Optional[int],
        action_type: str,
        amount: Decimal | float = 0.0,
        details: Optional[str] = None,
    ) -> None:
        payload = {
            "account_id": account_id,
            "action_type": action_type,
            "amount": float(amount),
            "details": (details or "")[:1000],
        }
        await self._request("POST", "stats_logs", json_body=payload)

    async def get_global_stats(self) -> GlobalStatsSummary:
        accounts = await self.list_accounts()
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        all_subs = await self._request("GET", "subscriptions", params={"select": "id,account_id,is_left"})
        today_logs = await self._request(
            "GET",
            "stats_logs",
            params={
                "select": "account_id,action_type,amount",
                "created_at": f"gte.{today_start}",
            },
        )

        subs_by_acc: Dict[int, int] = {}
        active_by_acc: Dict[int, int] = {}
        for row in all_subs or []:
            aid = row["account_id"]
            subs_by_acc[aid] = subs_by_acc.get(aid, 0) + 1
            if not row.get("is_left"):
                active_by_acc[aid] = active_by_acc.get(aid, 0) + 1

        today_subs_by_acc: Dict[int, int] = {}
        today_earned_by_acc: Dict[int, Decimal] = {}
        for row in today_logs or []:
            aid = row.get("account_id")
            if aid is None:
                continue
            atype = row.get("action_type")
            amt = Decimal(str(row.get("amount") or 0))
            if atype in ("subscribe", "mandatory_sub"):
                today_subs_by_acc[aid] = today_subs_by_acc.get(aid, 0) + 1
            if atype in ("subscribe", "bonus", "view_post"):
                today_earned_by_acc[aid] = today_earned_by_acc.get(aid, Decimal("0")) + amt

        per_account: List[AccountStatsSummary] = []
        total_balance = Decimal("0.00")
        total_today_subs = 0
        total_today_earned = Decimal("0.00")
        active_count = 0

        for acc in accounts:
            total_balance += acc.balance
            if acc.status == AccountStatus.ACTIVE:
                active_count += 1
            t_subs = today_subs_by_acc.get(acc.id, 0)
            t_earn = today_earned_by_acc.get(acc.id, Decimal("0.00"))
            total_today_subs += t_subs
            total_today_earned += t_earn
            per_account.append(
                AccountStatsSummary(
                    account_id=acc.id,
                    phone=acc.phone,
                    status=acc.status,
                    balance=acc.balance,
                    total_subs=subs_by_acc.get(acc.id, 0),
                    today_subs=t_subs,
                    today_earned=t_earn,
                    active_channels=active_by_acc.get(acc.id, 0),
                )
            )

        return GlobalStatsSummary(
            total_accounts=len(accounts),
            active_accounts=active_count,
            total_balance=total_balance,
            total_subs_all_time=len(all_subs or []),
            today_subs=total_today_subs,
            today_earned=total_today_earned,
            per_account=per_account,
        )

    # ------------------------------------------------------------------
    # DYNAMIC SETTINGS
    # ------------------------------------------------------------------
    async def get_settings(self, default_target_bot: str = "XObunachiBot") -> DynamicSettings:
        rows = await self._request("GET", "settings", params={"select": "key,value"})
        raw = {r["key"]: r["value"] for r in (rows or [])}
        return DynamicSettings.from_dict(raw, default_target_bot=default_target_bot)

    async def set_setting(self, key: str, value: str) -> None:
        await self._request(
            "POST",
            "settings",
            params={"on_conflict": "key"},
            json_body={"key": key, "value": str(value)},
            prefer="resolution=merge-duplicates",
        )

    # ------------------------------------------------------------------
    # FSM STATES (VERCEL SERVERLESS AIOGRAM STORAGE)
    # ------------------------------------------------------------------
    async def fsm_get(self, storage_key: str) -> Dict[str, Any]:
        rows = await self._request(
            "GET",
            "fsm_states",
            params={"select": "state,data", "storage_key": f"eq.{storage_key}", "limit": 1},
        )
        if not rows:
            return {"state": None, "data": {}}
        return {
            "state": rows[0].get("state"),
            "data": rows[0].get("data") or {},
        }

    async def fsm_upsert(
        self, storage_key: str, *, state: Optional[str], data: Dict[str, Any]
    ) -> None:
        await self._request(
            "POST",
            "fsm_states",
            params={"on_conflict": "storage_key"},
            json_body={
                "storage_key": storage_key,
                "state": state,
                "data": data,
            },
            prefer="resolution=merge-duplicates",
        )

    async def fsm_delete(self, storage_key: str) -> None:
        await self._request("DELETE", "fsm_states", params={"storage_key": f"eq.{storage_key}"})
