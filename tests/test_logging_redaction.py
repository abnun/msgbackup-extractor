"""Tests fuer den Redaction-Filter.

Der Filter ist der Backstop gegen versehentliches Loggen personenbezogener
Daten. Er ersetzt nicht die Disziplin, Inhalte gar nicht erst zu loggen -
diese Tests pruefen beides.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from msgbackup_extractor.core.logging_setup import (
    LOGGER_NAME,
    MASK,
    RedactionFilter,
    configure_logging,
    get_logger,
)


@pytest.fixture
def redactor() -> RedactionFilter:
    return RedactionFilter(show_paths=False)


# ---------------------------------------------------------------------------
# Maskierung
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Kontakt max.mustermann@example.com gefunden",
        "Absender a.b+tag@sub.domain.co.uk",
    ],
)
def test_email_addresses_are_masked(redactor: RedactionFilter, text: str) -> None:
    assert "@" not in redactor.redact(text)
    assert MASK in redactor.redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "Nummer +49 170 1234567",
        "Absender +41791234567",
        "Kontakt +1 (555) 123-4567",
    ],
)
def test_phone_numbers_are_masked(redactor: RedactionFilter, text: str) -> None:
    assert MASK in redactor.redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "password: hunter2",
        "Passwort=geheim123",
        "PASSPHRASE: abc-def",
        "secret = s3cr3t",
        "token: eyJhbGciOi",
    ],
)
def test_password_like_values_are_masked(redactor: RedactionFilter, text: str) -> None:
    cleaned = redactor.redact(text)
    assert MASK in cleaned
    for leak in ("hunter2", "geheim123", "abc-def", "s3cr3t", "eyJhbGciOi"):
        assert leak not in cleaned


def test_threema_id_is_masked(redactor: RedactionFilter) -> None:
    assert "ABCD1234" not in redactor.redact("Threema-ID: ABCD1234")


def test_paths_are_masked_but_extension_is_kept(redactor: RedactionFilter) -> None:
    cleaned = redactor.redact(
        "Extrahiert AppDomain-ch.threema.iapp/Documents/Max Mustermann/urlaub.jpg"
    )
    assert "Mustermann" not in cleaned
    assert "Documents" not in cleaned
    assert cleaned.endswith(".jpg")


def test_paths_are_kept_when_explicitly_allowed() -> None:
    text = "Extrahiert AppDomain-x/Documents/Max/urlaub.jpg"
    assert RedactionFilter(show_paths=True).redact(text) == text


def test_email_is_masked_even_when_paths_are_allowed() -> None:
    """Pfade freizugeben darf keine Kontaktdaten freigeben."""
    cleaned = RedactionFilter(show_paths=True).redact("Kontakt a@b.de")
    assert "a@b.de" not in cleaned


# ---------------------------------------------------------------------------
# Keine Fehltreffer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Extraction completed",
        "Successful: 12421  Failed: 17  Skipped: 0",
        "Total size: 4.82 GB",
        "SQLite databases: 3",
        "Protection class 3 nicht verfuegbar",
        "fileID c91efa0e2ed8d47e470a6049a106b70590884779",
    ],
)
def test_harmless_messages_pass_through_unchanged(
    redactor: RedactionFilter, text: str
) -> None:
    assert redactor.redact(text) == text


# ---------------------------------------------------------------------------
# Filterverhalten am Logger
# ---------------------------------------------------------------------------


def test_records_marked_sensitive_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    logger = configure_logging(verbose=True)
    handler = logger.handlers[0]
    record = logging.LogRecord(
        LOGGER_NAME, logging.INFO, __file__, 1, "Nachrichtentext", None, None
    )
    record.sensitive = True  # type: ignore[attr-defined]
    assert handler.filters[0].filter(record) is False


def test_filter_rewrites_the_record_message() -> None:
    logger = configure_logging()
    handler = logger.handlers[0]
    record = logging.LogRecord(
        LOGGER_NAME, logging.INFO, __file__, 1, "Kontakt %s", ("a@b.de",), None
    )
    assert handler.filters[0].filter(record) is True
    assert "a@b.de" not in record.getMessage()


def test_configure_logging_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    logger = configure_logging()
    logger.info("Testmeldung")
    captured = capsys.readouterr()
    assert "Testmeldung" in captured.err
    assert captured.out == ""


def test_configure_logging_replaces_handlers_on_repeated_calls() -> None:
    configure_logging()
    first = len(logging.getLogger(LOGGER_NAME).handlers)
    configure_logging()
    assert len(logging.getLogger(LOGGER_NAME).handlers) == first


def test_log_file_never_contains_plain_paths(tmp_path: Path) -> None:
    """Auch mit show_paths=True bleibt das Dateilog frei von Klartextpfaden."""
    log_file = tmp_path / "logs" / "run.log"
    logger = configure_logging(show_paths=True, log_file=log_file)
    logger.info("Extrahiert AppDomain-x/Documents/Max/urlaub.jpg")
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "Max" not in content
    assert MASK in content


def test_get_logger_returns_namespaced_child() -> None:
    assert get_logger("manifest").name == f"{LOGGER_NAME}.manifest"
    assert get_logger().name == LOGGER_NAME
