import pytest
from cryptography.fernet import Fernet

from app.security import SessionCipher
from app.services.proxy_checker import parse_proxy_url


def test_session_cipher_roundtrip():
    key = Fernet.generate_key().decode("utf-8")
    cipher = SessionCipher(key)
    assert cipher.enabled is True

    raw = "1BVtsOHoBu_example_telethon_string_session_xyz=="
    encrypted = cipher.encrypt(raw)
    assert encrypted.startswith(SessionCipher.PREFIX)
    assert encrypted != raw
    assert cipher.decrypt(encrypted) == raw


def test_session_cipher_disabled_passthrough():
    cipher = SessionCipher("")
    assert cipher.enabled is False
    raw = "plain_session_string"
    assert cipher.encrypt(raw) == raw
    assert cipher.decrypt(raw) == raw


def test_parse_proxy_url_valid_and_invalid():
    p = parse_proxy_url("socks5://user1:pass1@192.168.1.10:1080")
    assert p.protocol == "socks5"
    assert p.host == "192.168.1.10"
    assert p.port == 1080
    assert p.username == "user1"
    assert p.password == "pass1"

    p2 = parse_proxy_url("http://10.0.0.5:8080")
    assert p2.protocol == "http"
    assert p2.host == "10.0.0.5"
    assert p2.port == 8080
    assert p2.username is None

    with pytest.raises(ValueError):
        parse_proxy_url("ftp://1.2.3.4:21")

    with pytest.raises(ValueError):
        parse_proxy_url("socks5://invalid-no-port")
