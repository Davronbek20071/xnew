from decimal import Decimal

from app.db.models import AccountModel, AccountStatus, DynamicSettings, ProxyModel


def test_dynamic_settings_from_dict():
    s = DynamicSettings.from_dict(
        {
            "target_bot_username": "@XObunachiBot",
            "cycle_min_minutes": "22",
            "cycle_max_minutes": "28",
            "action_delay_min_sec": "4",
            "action_delay_max_sec": "9",
            "auto_leave_days": "16",
            "max_tasks_per_cycle": "30",
        }
    )
    assert s.target_bot_username == "XObunachiBot"
    assert s.cycle_min_minutes == 22
    assert s.cycle_max_minutes == 28
    assert s.action_delay_min_sec == 4.0
    assert s.action_delay_max_sec == 9.0
    assert s.auto_leave_days == 16
    assert s.max_tasks_per_cycle == 30


def test_dynamic_settings_enforces_safe_bounds():
    s = DynamicSettings.from_dict(
        {
            "cycle_min_minutes": "25",
            "cycle_max_minutes": "10",
            "auto_leave_days": "5",
        }
    )
    assert s.cycle_max_minutes == 25
    assert s.auto_leave_days == 15


def test_account_and_proxy_models():
    proxy = ProxyModel(
        id=1, protocol="socks5", host="1.2.3.4", port=1080, username="u", password="secret"
    )
    assert proxy.masked_url() == "socks5://u:***@1.2.3.4:1080"
    assert proxy.full_url() == "socks5://u:secret@1.2.3.4:1080"

    acc = AccountModel(
        id=1,
        phone="+998901234567",
        session_name="acc_998901234567",
        session_string="abc",
        api_id=12345,
        api_hash="hash",
        status=AccountStatus.ACTIVE,
        balance=Decimal("15.50"),
    )
    assert acc.status_emoji == "🟢 Ishlamoqda"
