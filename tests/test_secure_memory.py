"""Tests fuer den Umgang mit Schluesselmaterial im Speicher."""

from __future__ import annotations

import pytest

from msgbackup_extractor.core.secure_memory import (
    REDACTED,
    SecretBytes,
    SecretWiped,
    transient_password,
)

SECRET = b"streng-geheimes-schluesselmaterial"


def test_reveal_returns_the_value() -> None:
    assert SecretBytes(SECRET).reveal() == SECRET


def test_length_is_available_without_revealing() -> None:
    assert len(SecretBytes(SECRET)) == len(SECRET)


def test_repr_str_and_format_never_leak_the_value() -> None:
    secret = SecretBytes(SECRET)
    for rendering in (repr(secret), str(secret), f"{secret}", f"{secret!s}", f"{secret}"):
        assert SECRET.decode() not in rendering
        assert REDACTED in rendering


def test_wipe_makes_the_value_inaccessible() -> None:
    secret = SecretBytes(SECRET)
    secret.wipe()
    assert secret.is_wiped
    with pytest.raises(SecretWiped):
        secret.reveal()
    with pytest.raises(SecretWiped):
        len(secret)


def test_wipe_is_idempotent() -> None:
    secret = SecretBytes(SECRET)
    secret.wipe()
    secret.wipe()
    assert secret.is_wiped


def test_wipe_zeroes_the_underlying_buffer() -> None:
    """Der Puffer wird tatsaechlich ueberschrieben, nicht nur dereferenziert."""
    buffer = bytearray(SECRET)
    secret = SecretBytes(buffer)
    inner = secret._buffer
    assert inner is not None
    secret.wipe()
    assert bytes(inner) == b""


def test_context_manager_wipes_on_exit() -> None:
    with SecretBytes(SECRET) as secret:
        assert secret.reveal() == SECRET
    assert secret.is_wiped


def test_context_manager_wipes_even_on_exception() -> None:
    secret = SecretBytes(SECRET)
    with pytest.raises(RuntimeError), secret:
        raise RuntimeError("boom")
    assert secret.is_wiped


def test_equality_is_constant_time_and_type_strict() -> None:
    assert SecretBytes(SECRET) == SecretBytes(SECRET)
    assert SecretBytes(SECRET) != SecretBytes(b"anders")
    assert SecretBytes(SECRET) != SECRET  # kein Vergleich mit rohen Bytes


def test_secret_is_unhashable() -> None:
    """Hashbarkeit wuerde ein Leck in Dicts und Sets ermoeglichen."""
    with pytest.raises(TypeError):
        hash(SecretBytes(SECRET))


def test_view_gives_readonly_access() -> None:
    secret = SecretBytes(SECRET)
    view = secret.view()
    assert view.readonly
    assert bytes(view) == SECRET


def test_empty_secret_can_be_wiped() -> None:
    secret = SecretBytes(b"")
    secret.wipe()
    assert secret.is_wiped


def test_transient_password_uses_getpass_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        calls.append(prompt)
        return "geheim"

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    with transient_password("Password: ") as password:
        assert password == "geheim"
    assert calls == ["Password: "]


def test_transient_password_rejects_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    with pytest.raises(ValueError, match="kein Passwort"), transient_password():
        pass
