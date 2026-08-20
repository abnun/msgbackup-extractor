"""Tests fuer das Signal-Profil.

Signal schliesst sein Datenverzeichnis vom iOS-Backup aus. Das Profil kann
deshalb nichts extrahieren - seine Aufgabe ist, genau das zu sagen, damit ein
leeres Ergebnis nicht wie ein Fehler des Programms aussieht.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from msgbackup_extractor.apps.base import MediaContext
from msgbackup_extractor.apps.registry import get_profile
from msgbackup_extractor.apps.signal import SIGNAL_TABLE_CANDIDATES, SignalProfile
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.sqlite_ro import describe_database, open_readonly
from msgbackup_extractor.models import DetectionStatus
from tests.support.backup_builder import BuiltBackup


def _context(path: Path) -> MediaContext:
    with open_readonly(path) as connection:
        return MediaContext(
            connection=connection,
            schemas=describe_database(connection, count_rows=False),
            external_files={},
            entries_by_path={},
        )


def test_signal_is_registered() -> None:
    assert isinstance(get_profile("signal"), SignalProfile)


def test_signal_is_detected(signal_backup: BuiltBackup) -> None:
    result = SignalProfile().detect(AppleBackup(signal_backup.path).info())
    assert result.status is DetectionStatus.CONFIRMED
    assert result.bundle_id == "org.whispersystems.signal"
    assert result.bundle_version == "1799.0"


def test_absent_database_yields_an_explanation(tmp_path: Path) -> None:
    """Der Grund gehoert in den Bericht, nicht ein stilles leeres Ergebnis."""
    path = tmp_path / "fremd.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with open_readonly(path) as ro:
        context = MediaContext(
            connection=ro,
            schemas=describe_database(ro, count_rows=False),
            external_files={},
            entries_by_path={},
        )
        result = SignalProfile().enumerate_media(context)

    assert not result.is_supported
    reason = result.unsupported_reason or ""
    assert "schliesst sein Datenverzeichnis vom iOS-Backup aus" in reason
    assert "kein Fehler dieses" in reason
    assert result.items == ()


def test_foreign_database_is_not_claimed_as_signal_data(tmp_path: Path) -> None:
    from msgbackup_extractor.apps.base import DatabaseCandidate
    from msgbackup_extractor.models import ManifestEntry

    entry = ManifestEntry(
        file_id="a" * 40,
        domain="AppDomain-org.whispersystems.signal",
        relative_path="Library/WebKit/WebsiteData/ResourceLoadStatistics/observations.db",
        kind=None,
    )
    roles = SignalProfile().classify_databases(
        (DatabaseCandidate(entry=entry, tables=("observations", "sqlite_sequence")),)
    )
    assert roles[0].role == "unknown"
    assert "Apple-Frameworks" in roles[0].reason


def test_present_tables_are_reported_as_unexpected(tmp_path: Path) -> None:
    """Sollte Signal das Ausschliessen aufgeben, soll das auffallen."""
    path = tmp_path / "signal.sqlite"
    connection = sqlite3.connect(path)
    for table in SIGNAL_TABLE_CANDIDATES:
        connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()

    with open_readonly(path) as ro:
        context = MediaContext(
            connection=ro,
            schemas=describe_database(ro, count_rows=False),
            external_files={},
            entries_by_path={},
        )
        result = SignalProfile().enumerate_media(context)

    assert not result.is_supported
    assert any("Unerwartet" in note for note in result.notes)
    assert "nicht implementiert" in (result.unsupported_reason or "")
