from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    """Telethon StringSession'ni Supabase'da shifrlab saqlash uchun yordamchi."""

    PREFIX = "enc::"

    def __init__(self, key: str = "") -> None:
        cleaned = (key or "").strip()
        self._fernet = Fernet(cleaned.encode("utf-8")) if cleaned else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, raw_session: str) -> str:
        if not raw_session:
            return ""
        if not self._fernet:
            return raw_session
        if raw_session.startswith(self.PREFIX):
            return raw_session
        token = self._fernet.encrypt(raw_session.encode("utf-8")).decode("utf-8")
        return f"{self.PREFIX}{token}"

    def decrypt(self, stored_session: str) -> str:
        if not stored_session:
            return ""
        if not stored_session.startswith(self.PREFIX):
            return stored_session
        if not self._fernet:
            raise ValueError(
                "Sessiya shifrlangan, lekin SESSION_ENCRYPTION_KEY kiritilmagan!"
            )
        token = stored_session[len(self.PREFIX) :]
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("SESSION_ENCRYPTION_KEY noto'g'ri yoki sessiya buzilgan.") from exc
