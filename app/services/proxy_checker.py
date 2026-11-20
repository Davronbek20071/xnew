from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from app.db.models import ProxyModel


@dataclass
class ParsedProxy:
    protocol: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None


def parse_proxy_url(raw_url: str) -> ParsedProxy:
    """`socks5://user:pass@ip:port` yoki `http://ip:port` formatidagi proksini ajratadi."""
    text = (raw_url or "").strip()
    if "://" not in text:
        text = f"socks5://{text}"
    parsed = urlparse(text)
    protocol = (parsed.scheme or "socks5").lower()
    if protocol not in {"socks5", "socks4", "http"}:
        raise ValueError(
            "Proksi protokoli faqat socks5, socks4 yoki http bo'lishi kerak."
        )
    if not parsed.hostname or not parsed.port:
        raise ValueError(
            "Proksi manzili noto'g'ri. Namuna: socks5://user:pass@1.2.3.4:1080"
        )
    return ParsedProxy(
        protocol=protocol,
        host=parsed.hostname,
        port=int(parsed.port),
        username=parsed.username,
        password=parsed.password,
    )


def build_telethon_proxy(proxy: Optional[ProxyModel]) -> Optional[Dict[str, Any]]:
    """Telethon (python-socks) uchun proksi lug'atini tayyorlaydi."""
    if not proxy or not proxy.is_active:
        return None
    try:
        from python_socks import ProxyType
    except ImportError:
        return None

    type_map = {
        "socks5": ProxyType.SOCKS5,
        "socks4": ProxyType.SOCKS4,
        "http": ProxyType.HTTP,
    }
    proxy_type = type_map.get(proxy.protocol.lower(), ProxyType.SOCKS5)
    cfg: Dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": proxy.host,
        "port": proxy.port,
        "rdns": True,
    }
    if proxy.username:
        cfg["username"] = proxy.username
    if proxy.password:
        cfg["password"] = proxy.password
    return cfg


async def check_proxy_connectivity(
    proxy: ParsedProxy | ProxyModel, timeout: float = 6.0
) -> Tuple[bool, str]:
    """Proksi orqali Telegram DC (149.154.167.50:443) ga ulanishni tekshiradi."""
    try:
        from python_socks.async_.asyncio import Proxy as AsyncProxy
        from python_socks import ProxyType

        type_map = {
            "socks5": ProxyType.SOCKS5,
            "socks4": ProxyType.SOCKS4,
            "http": ProxyType.HTTP,
        }
        p = AsyncProxy.create(
            proxy_type=type_map.get(proxy.protocol.lower(), ProxyType.SOCKS5),
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
            rdns=True,
        )
        sock = await asyncio.wait_for(
            p.connect(dest_host="149.154.167.50", dest_port=443),
            timeout=timeout,
        )
        try:
            sock.close()
        except Exception:
            pass
        return True, "Proksi ishlayapti (Telegram DC ulanishi muvaffaqiyatli)."
    except Exception as exc:
        return False, f"Proksi ulanish xatosi: {exc}"
