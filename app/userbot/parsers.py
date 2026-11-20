from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class TelegramTarget:
    """Kanal yoki guruh havolasi tahlil natijasi."""

    kind: str  # "public" | "private"
    value: str  # username (without @) or invite hash
    raw: str


# Telegram havolalari: https://t.me/+hash, t.me/joinchat/hash, t.me/username, @username
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_PUBLIC_URL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#]|$)",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})\b")

# X Obunachi [Bot] popup/xabar balans formatlari:
# "11.50 ₽ + 1 ₽ = 12.5 ₽" yoki "Sizga 1.4 ₽ bonus berildi"
_ALERT_EQUATION_RE = re.compile(
    r"([\d.,]+)\s*(?:₽|rub|руб|so['’`ʻʼ]?m|uzs)?\s*\+\s*([\d.,]+)\s*(?:₽|rub|руб|so['’`ʻʼ]?m|uzs)?\s*=\s*([\d.,]+)",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT_RE = re.compile(
    r"([\d]+(?:[.,][\d]+)?)\s*(?:₽|rub|руб|so['’`ʻʼ]?m|uzs)",
    re.IGNORECASE,
)

# Barcha o'zbekcha tutuq/apostrof Unicode variantlari (U+02BB, U+02BC, U+2018, U+2019, va h.k.)
_APOSTROPHE_RE = re.compile(r"[‘’ʻʼ`´′‛ʹˈ]")

# "Kanallar tugadi" kalit so'zlari
NO_MORE_CHANNELS_KEYWORDS = (
    "tugadi",
    "mavjud emas",
    "kanal yo'q",
    "kanallar yo'q",
    "hozircha kanal",
    "boshqa kanal",
    "vazifalar tugadi",
    "закончились",
    "нет доступных",
    "пока нет каналов",
)

# Majburiy obuna kalit so'zlari
MANDATORY_SUB_KEYWORDS = (
    "majburiy",
    "obuna bo'ling",
    "a'zo bo'ling",
    "azo bo'ling",
    "foydalanish uchun",
    "подпишитесь",
)


def parse_telegram_target(raw_text: str) -> Optional[TelegramTarget]:
    """Matn yoki URL dan birinchi Telegram kanal/guruh manzilini ajratadi."""
    if not raw_text:
        return None
    text = raw_text.strip()

    m_inv = _INVITE_RE.search(text)
    if m_inv:
        return TelegramTarget(kind="private", value=m_inv.group(1), raw=m_inv.group(0))

    m_pub = _PUBLIC_URL_RE.search(text)
    if m_pub:
        username = m_pub.group(1)
        if username.lower() not in {"joinchat", "addlist", "share", "proxy", "socks", "c"}:
            return TelegramTarget(kind="public", value=username, raw=m_pub.group(0))

    m_at = _MENTION_RE.search(text)
    if m_at:
        return TelegramTarget(kind="public", value=m_at.group(1), raw=f"@{m_at.group(1)}")

    return None


def extract_all_telegram_targets(urls: List[str], fallback_text: str = "") -> List[TelegramTarget]:
    """Inline URL tugmalar va xabar matnidan barcha noyob kanal havolalarini yig'adi."""
    seen: set[Tuple[str, str]] = set()
    targets: List[TelegramTarget] = []

    for url in urls:
        t = parse_telegram_target(url)
        if t and (t.kind, t.value.lower()) not in seen:
            seen.add((t.kind, t.value.lower()))
            targets.append(t)

    if not targets and fallback_text:
        for m in _INVITE_RE.finditer(fallback_text):
            key = ("private", m.group(1).lower())
            if key not in seen:
                seen.add(key)
                targets.append(TelegramTarget(kind="private", value=m.group(1), raw=m.group(0)))
        for m in _PUBLIC_URL_RE.finditer(fallback_text):
            uname = m.group(1)
            if uname.lower() in {"joinchat", "addlist", "share", "proxy", "socks", "c"}:
                continue
            key = ("public", uname.lower())
            if key not in seen:
                seen.add(key)
                targets.append(TelegramTarget(kind="public", value=uname, raw=m.group(0)))
        for m in _MENTION_RE.finditer(fallback_text):
            key = ("public", m.group(1).lower())
            if key not in seen:
                seen.add(key)
                targets.append(TelegramTarget(kind="public", value=m.group(1), raw=f"@{m.group(1)}"))

    return targets


def _to_decimal(num_str: str) -> Decimal:
    return Decimal(num_str.replace(",", ".").strip())


def parse_balance_alert(alert_text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Callback alert matnidan (earned_amount, new_balance) ni ajratadi.

    Namuna: '11.50 ₽ + 1 ₽ = 12.5 ₽' -> (Decimal('1'), Decimal('12.5'))
    """
    if not alert_text:
        return None, None
    m = _ALERT_EQUATION_RE.search(alert_text)
    if m:
        earned = _to_decimal(m.group(2))
        new_balance = _to_decimal(m.group(3))
        return earned, new_balance

    amounts = [_to_decimal(x) for x in _CURRENCY_AMOUNT_RE.findall(alert_text)]
    if len(amounts) >= 2:
        return amounts[0], amounts[-1]
    if len(amounts) == 1:
        return amounts[0], None
    return None, None


def parse_bonus_amount(text: str) -> Optional[Decimal]:
    """Kunlik bonus javob xabaridan olingan summani ajratadi (masalan: '1.4 ₽')."""
    if not text:
        return None
    m = _CURRENCY_AMOUNT_RE.search(text)
    if m:
        return _to_decimal(m.group(1))
    return None


def normalize_button_text(text: str) -> str:
    """Tugma va xabar matnini taqqoslash uchun barcha apostroflarni yagona `'` ga keltiradi."""
    cleaned = _APOSTROPHE_RE.sub("'", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def is_no_more_channels(text: str) -> bool:
    low = normalize_button_text(text)
    return any(kw in low for kw in NO_MORE_CHANNELS_KEYWORDS)


def is_mandatory_sub_message(text: str, button_texts: List[str]) -> bool:
    low = normalize_button_text(text)
    has_kw = any(kw in low for kw in MANDATORY_SUB_KEYWORDS)
    has_confirm_btn = any(
        any(k in normalize_button_text(b) for k in ("tasdiq", "tekshir", "провер", "confirm"))
        for b in button_texts
    )
    return has_kw and has_confirm_btn
