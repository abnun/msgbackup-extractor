"""Tests fuer Manifest.db-Parsing, Schema-Introspektion und MBFile-Dekodierung."""

from __future__ import annotations

import plistlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from msgbackup_extractor.core.manifest import (
    ManifestReader,
    ManifestSchemaError,
    decode_mbfile,
    resolve_layout,
)
from msgbackup_extractor.core.sqlite_ro import describe_database, open_readonly
from msgbackup_extractor.models import FileKind
from tests.conftest import THREEMA_APP_DOMAIN, sample_files
from tests.support.backup_builder import (
    EXTENDED_FILES_SCHEMA,
    UNKNOWN_SCHEMA,
    BackupFile,
    BuiltBackup,
    build_backup,
    build_mbfile_blob,
)

# ---------------------------------------------------------------------------
# MBFile-Dekodierung
# ---------------------------------------------------------------------------


def test_decode_mbfile_reads_all_fields() -> None:
    modified = datetime(2025, 3, 14, 18, 42, 11, tzinfo=UTC)
    entry = BackupFile(
        THREEMA_APP_DOMAIN, "Documents/a.jpg", b"x" * 123, protection_class=3,
        last_modified=modified,
    )
    result = decode_mbfile(build_mbfile_blob(entry, b"\x03\x00\x00\x00" + b"k" * 40))
    assert result.size == 123
    assert result.protection_class == 3
    assert result.relative_path == "Documents/a.jpg"
    assert result.mode == 0o100644
    assert result.flags == 1
    assert result.encryption_key == b"k" * 40
    assert result.last_modified == modified


def test_decode_mbfile_without_encryption_key() -> None:
    entry = BackupFile(THREEMA_APP_DOMAIN, "Documents/a.jpg", b"x" * 10)
    result = decode_mbfile(build_mbfile_blob(entry, None))
    assert result.encryption_key is None
    assert result.size == 10


def test_decode_mbfile_strips_class_prefix_from_key() -> None:
    """Die ersten vier Byte sind die Protection Class, nicht Teil des Schluessels."""
    entry = BackupFile(THREEMA_APP_DOMAIN, "Documents/a.jpg", b"x")
    blob = build_mbfile_blob(entry, b"\x04\x00\x00\x00" + bytes(range(40)))
    assert decode_mbfile(blob).encryption_key == bytes(range(40))


@pytest.mark.parametrize(
    "blob",
    [
        None,
        b"",
        b"kein plist",
        plistlib.dumps({"nicht": "nskeyedarchiver"}, fmt=plistlib.FMT_BINARY),
        plistlib.dumps([1, 2, 3], fmt=plistlib.FMT_BINARY),
    ],
)
def test_decode_mbfile_rejects_invalid_blobs(blob: bytes | None) -> None:
    with pytest.raises(ValueError):
        decode_mbfile(blob)


def test_decode_mbfile_rejects_too_short_encryption_key() -> None:
    entry = BackupFile(THREEMA_APP_DOMAIN, "Documents/a.jpg", b"x")
    with pytest.raises(ValueError, match="zu kurz"):
        decode_mbfile(build_mbfile_blob(entry, b"\x03\x00"))


@pytest.mark.parametrize("raw", [0, -1, True, False, "text", None, 10**12])
def test_implausible_timestamps_become_none(raw: object) -> None:
    """Eine erfundene Zeit waere schlimmer als keine Zeit."""
    plist = plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": ["$null", {"Size": 1, "LastModified": raw}],
        },
        fmt=plistlib.FMT_BINARY,
    )
    assert decode_mbfile(plist).last_modified is None


def test_epochs_are_distinct() -> None:
    """Die beiden Bezugszeitpunkte duerfen nicht verwechselt werden."""
    from msgbackup_extractor.core.manifest import APPLE_EPOCH, MBFILE_EPOCH

    assert APPLE_EPOCH.isoformat() == "2001-01-01T00:00:00+00:00"
    assert MBFILE_EPOCH.isoformat() == "1970-01-01T00:00:00+00:00"
    assert APPLE_EPOCH != MBFILE_EPOCH


# ---------------------------------------------------------------------------
# Schema-Aufloesung
# ---------------------------------------------------------------------------


def _layout_for(path: Path):
    with open_readonly(path) as connection:
        return resolve_layout(describe_database(connection))


def test_standard_schema_is_resolved(plain_backup: BuiltBackup) -> None:
    layout = _layout_for(plain_backup.path / "Manifest.db")
    assert layout.table == "Files"
    assert layout.columns["fileID"] == "fileID"
    assert layout.has_metadata_blob
    assert layout.has_flags


def test_extra_columns_do_not_break_resolution(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b",
        [BackupFile(THREEMA_APP_DOMAIN, "Documents/a.jpg", b"x")],
        schema=EXTENDED_FILES_SCHEMA,
        installed_applications=[],
    )
    layout = _layout_for(backup.path / "Manifest.db")
    assert layout.table == "Files"
    assert "extraColumn" in layout.schema.columns


def test_unknown_schema_raises_with_diagnostics(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b", [], schema=UNKNOWN_SCHEMA, installed_applications=[]
    )
    with pytest.raises(ManifestSchemaError) as error:
        _layout_for(backup.path / "Manifest.db")
    assert "keine Dateitabelle" in str(error.value)
    assert "SomethingElse" in str(error.value)
    assert "SomethingElse" in error.value.schemas


def test_missing_required_column_raises(tmp_path: Path) -> None:
    schema = """
    CREATE TABLE Files (
        fileID TEXT PRIMARY KEY,
        domain TEXT,
        flags INTEGER
    )
    """
    backup = build_backup(tmp_path / "b", [], schema=schema, installed_applications=[])
    with pytest.raises(ManifestSchemaError, match="relativePath"):
        _layout_for(backup.path / "Manifest.db")


def test_column_names_are_matched_case_insensitively(tmp_path: Path) -> None:
    schema = """
    CREATE TABLE FILES (
        FILEID TEXT PRIMARY KEY,
        DOMAIN TEXT,
        RELATIVEPATH TEXT,
        FLAGS INTEGER,
        FILE BLOB
    )
    """
    path = tmp_path / "Manifest.db"
    connection = sqlite3.connect(path)
    connection.execute(schema)
    connection.commit()
    connection.close()
    layout = _layout_for(path)
    assert layout.columns["fileID"] == "FILEID"
    assert layout.columns["relativePath"] == "RELATIVEPATH"


def test_missing_optional_columns_degrade_gracefully(tmp_path: Path) -> None:
    schema = """
    CREATE TABLE Files (
        fileID TEXT PRIMARY KEY,
        domain TEXT,
        relativePath TEXT
    )
    """
    path = tmp_path / "Manifest.db"
    connection = sqlite3.connect(path)
    connection.execute(schema)
    connection.execute(
        "INSERT INTO Files (fileID, domain, relativePath) VALUES (?, ?, ?)",
        ("ab" + "0" * 38, "AppDomain-x", "Documents/a.jpg"),
    )
    connection.commit()
    connection.close()
    layout = _layout_for(path)
    assert not layout.has_metadata_blob
    assert not layout.has_flags


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_reader_yields_all_entries(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        entries = list(reader.entries())
    assert len(entries) == len(plain_backup.files)


def test_reader_reports_kind(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        by_path = {entry.relative_path: entry for entry in reader.entries()}
    assert by_path["Documents/img/photo1.jpg"].kind is FileKind.FILE
    assert by_path["Documents/img"].kind is FileKind.DIRECTORY


def test_reader_reports_size_and_protection_class(plain_backup: BuiltBackup) -> None:
    expected = plain_backup.file_by_path("Documents/img/photo1.jpg")
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        entry = next(
            e for e in reader.entries() if e.relative_path == "Documents/img/photo1.jpg"
        )
    assert entry.size == expected.size
    assert entry.protection_class == 3


def test_corrupt_blob_yields_decode_error_and_continues(plain_backup: BuiltBackup) -> None:
    """Eine kaputte Zeile darf den Lauf nicht verlieren."""
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        entries = list(reader.entries())
    broken = [e for e in entries if e.decode_error]
    assert len(broken) == 1
    assert broken[0].relative_path == "Documents/kaputte-metadaten.jpg"
    assert broken[0].size is None
    assert len(entries) == len(plain_backup.files)


def test_reader_filters_by_domain(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        entries = list(reader.entries(domains=(THREEMA_APP_DOMAIN,)))
    assert entries
    assert {entry.domain for entry in entries} == {THREEMA_APP_DOMAIN}


def test_reader_with_empty_domain_filter_yields_nothing(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        assert list(reader.entries(domains=())) == []


def test_domain_names_are_sorted_and_unique(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        domains = reader.domain_names()
    assert list(domains) == sorted(set(domains))
    assert THREEMA_APP_DOMAIN in domains


def test_statistics_aggregate_correctly(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        statistics = reader.statistics()
    files = [f for f in plain_backup.files if f.flags == 1]
    assert statistics.total_entries == len(plain_backup.files)
    assert statistics.files == len(files)
    assert statistics.directories == 1
    assert statistics.decode_errors == 1
    assert statistics.encrypted_entries == 0


def test_statistics_contain_no_paths(plain_backup: BuiltBackup) -> None:
    """Die Statistik ist aggregiert - Domains ja, Pfade nein."""
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        statistics = reader.statistics()
    rendered = repr(statistics)
    assert "photo1.jpg" not in rendered
    assert "Documents" not in rendered


def test_encrypted_backup_entries_carry_keys(tmp_path: Path) -> None:
    """Bei verschluesselten Backups steht der Wrapped Key im Manifest-Eintrag."""
    from tests.support.backup_builder import aes_cbc_encrypt  # noqa: F401  (Dokumentation)

    backup = build_backup(
        tmp_path / "e",
        sample_files(),
        password="pw",
        encrypt_manifest=False,  # Manifest im Klartext, Nutzdaten verschluesselt
        installed_applications=[],
    )
    with ManifestReader(backup.path / "Manifest.db") as reader:
        statistics = reader.statistics()
        entry = next(
            e for e in reader.entries() if e.relative_path == "Documents/img/photo1.jpg"
        )
    assert entry.encryption_key is not None
    assert len(entry.encryption_key) == 40
    assert entry.is_encrypted
    assert statistics.encrypted_entries > 0


def test_reader_requires_context_manager(plain_backup: BuiltBackup) -> None:
    reader = ManifestReader(plain_backup.path / "Manifest.db")
    with pytest.raises(RuntimeError, match="Context Manager"):
        reader.count()


def test_count_matches_entries(plain_backup: BuiltBackup) -> None:
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        assert reader.count() == len(list(reader.entries()))


# ---------------------------------------------------------------------------
# Epoche der Zeitstempel
# ---------------------------------------------------------------------------


def test_mbfile_uses_the_unix_epoch() -> None:
    """MBFile zaehlt ab 1970, nicht ab 2001.

    Am echten Backup verifiziert: 4000 von 4000 Werten ergeben mit der
    Unix-Epoche ein plausibles Datum, mit der Apple-Epoche kein einziges - dort
    landen sie 31 Jahre in der Zukunft.
    """
    from msgbackup_extractor.core.manifest import MBFILE_EPOCH, mbfile_timestamp

    assert datetime(1970, 1, 1, tzinfo=UTC) == MBFILE_EPOCH
    # 1694890024 ist 2023-09-16, nicht 2054-09-16.
    assert mbfile_timestamp(1694890024) == datetime(2023, 9, 16, 18, 47, 4, tzinfo=UTC)


def test_future_timestamps_are_rejected() -> None:
    """Eine Datei kann nicht in der Zukunft geaendert worden sein.

    Diese Pruefung ist der Grund, warum eine verwechselte Epoche sofort
    auffaellt statt erst in Jahrzehnten.
    """
    from msgbackup_extractor.core.manifest import mbfile_timestamp

    now = datetime(2026, 8, 20, tzinfo=UTC)
    # Derselbe Rohwert, faelschlich als Apple-Epoche gelesen, ergaebe 2054.
    apple_misread = int(
        (datetime(2054, 9, 16, tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
    )
    assert mbfile_timestamp(apple_misread, now=now) is None
    # Ein Tag Spielraum fuer Uhrenabweichungen bleibt erlaubt.
    slack = int((now - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) + 3600
    assert mbfile_timestamp(slack, now=now) is not None


def test_timestamps_before_the_first_iphone_are_rejected() -> None:
    from msgbackup_extractor.core.manifest import mbfile_timestamp

    before = int(
        (datetime(1990, 1, 1, tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
    )
    assert mbfile_timestamp(before) is None


def test_core_data_uses_the_apple_epoch() -> None:
    """Core Data zaehlt ab 2001 - die andere Epoche als MBFile."""
    from msgbackup_extractor.apps.threema import _apple_datetime

    now = datetime(2026, 8, 20, tzinfo=UTC)
    seconds = int(
        (datetime(2025, 3, 14, 18, 42, 11, tzinfo=UTC) - datetime(2001, 1, 1, tzinfo=UTC))
        .total_seconds()
    )
    assert _apple_datetime(seconds, now=now) == datetime(
        2025, 3, 14, 18, 42, 11, tzinfo=UTC
    )


def test_core_data_rejects_future_timestamps() -> None:
    from msgbackup_extractor.apps.threema import _apple_datetime

    now = datetime(2026, 8, 20, tzinfo=UTC)
    seconds = int(
        (datetime(2040, 1, 1, tzinfo=UTC) - datetime(2001, 1, 1, tzinfo=UTC)).total_seconds()
    )
    assert _apple_datetime(seconds, now=now) is None


def test_backup_timestamps_are_plausible(plain_backup: BuiltBackup) -> None:
    """Ende-zu-Ende: die Zeitstempel des Fixtures kommen richtig heraus."""
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        entry = next(
            e for e in reader.entries() if e.relative_path == "Documents/img/photo1.jpg"
        )
    assert entry.last_modified is not None
    assert entry.last_modified.year == 2025
    assert entry.last_modified.month == 3
    assert entry.last_modified.day == 14
