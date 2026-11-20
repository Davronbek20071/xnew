from decimal import Decimal

from app.userbot.parsers import (
    extract_all_telegram_targets,
    is_mandatory_sub_message,
    is_no_more_channels,
    parse_balance_alert,
    parse_bonus_amount,
    parse_telegram_target,
)


def test_parse_public_and_private_targets():
    pub = parse_telegram_target("https://t.me/uzbek_news_channel")
    assert pub is not None
    assert pub.kind == "public"
    assert pub.value == "uzbek_news_channel"

    mention = parse_telegram_target("Kanal: @my_super_bot_test")
    assert mention is not None
    assert mention.kind == "public"
    assert mention.value == "my_super_bot_test"

    priv1 = parse_telegram_target("https://t.me/+AbCdEfGh123456")
    assert priv1 is not None
    assert priv1.kind == "private"
    assert priv1.value == "AbCdEfGh123456"

    priv2 = parse_telegram_target("https://t.me/joinchat/ZzYyXxWw112233")
    assert priv2 is not None
    assert priv2.kind == "private"
    assert priv2.value == "ZzYyXxWw112233"


def test_extract_all_targets_deduplicates():
    urls = [
        "https://t.me/channel_one",
        "https://t.me/channel_one",
        "https://t.me/+InviteHash9999",
    ]
    targets = extract_all_telegram_targets(urls)
    assert len(targets) == 2
    assert targets[0].value == "channel_one"
    assert targets[1].value == "InviteHash9999"


def test_parse_balance_alert_equation():
    earned, new_bal = parse_balance_alert("11.50 ₽ + 1 ₽ = 12.5 ₽")
    assert earned == Decimal("1")
    assert new_bal == Decimal("12.5")

    earned2, new_bal2 = parse_balance_alert("0,50 ₽ + 1,25 ₽ = 1,75 ₽")
    assert earned2 == Decimal("1.25")
    assert new_bal2 == Decimal("1.75")


def test_parse_bonus_amount():
    assert parse_bonus_amount("Tabriklaymiz! Kunlik bonus: 1.4 ₽ berildi.") == Decimal("1.4")
    assert parse_bonus_amount("Bonus allaqachon olingan") is None


def test_mandatory_and_no_more_channels_detection():
    assert is_mandatory_sub_message(
        "Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:",
        ["1-kanal", "✅ Tasdiqlash"],
    )
    assert not is_mandatory_sub_message("Asosiy menyu", ["Pul ishlash", "Bonus"])
    assert is_no_more_channels("Hozircha mavjud kanallar tugadi, keyinroq urinib ko'ring.")
