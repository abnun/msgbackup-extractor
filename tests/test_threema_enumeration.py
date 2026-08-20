"""Tests fuer die Threema-Medien-Enumeration.

Das Fixture bildet die am echten Backup vermessene Struktur nach: zwei
Blob-Quellen, verwaiste Rueckrichtung bei ZIMAGEDATA, Beziehungen auf der
Nachrichtenseite, und Luecken bei Namen, Datum und Dateien.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msgbackup_extractor.apps.base import MediaContext
from msgbackup_extractor.apps.threema import (
    ThreemaProfile,
    external_reference_name,
    is_external_reference,
)
from msgbackup_extractor.core.manifest import ManifestReader
from msgbackup_extractor.core.sqlite_ro import describe_database, open_readonly
from msgbackup_extractor.models import FileKind, SourceKind
from tests.conftest import ThreemaBackup
from tests.support.threema_fixture import (
    DATABASE_NAME,
    EXPECTED_CHAT_NAMES,
    EXTERNAL_DIR,
    external_reference,
)


def _enumerate(target: ThreemaBackup):
    with ManifestReader(target.path / "Manifest.db") as reader:
        entries = [e for e in reader.entries() if e.kind is FileKind.FILE]
    by_path = {e.relative_path: e for e in entries}
    external = {
        e.basename: e for e in entries if e.relative_path.startswith(f"{EXTERNAL_DIR}/")
    }
    db_entry = by_path[DATABASE_NAME]
    db_path = target.path / db_entry.file_id[:2] / db_entry.file_id
    with open_readonly(db_path) as connection:
        context = MediaContext(
            connection=connection,
            schemas=describe_database(connection, count_rows=False),
            external_files=external,
            entries_by_path=by_path,
        )
        return ThreemaProfile().enumerate_media(context)


# ---------------------------------------------------------------------------
# Referenzformat
# ---------------------------------------------------------------------------


def test_external_reference_is_recognised() -> None:
    blob = external_reference("ABCDEF01-2345-6789-ABCD-EF0123456789")
    assert len(blob) == 38
    assert is_external_reference(blob)
    assert external_reference_name(blob) == "ABCDEF01-2345-6789-ABCD-EF0123456789"


@pytest.mark.parametrize(
    "blob",
    [
        None,
        b"",
        b"\x02" + b"A" * 35 + b"\x00",  # 37 Byte
        b"\x02" + b"A" * 37 + b"\x00",  # 39 Byte
        b"\x03" + b"A" * 36 + b"\x00",  # falsches Praefix
        b"\x02" + b"A" * 36 + b"\x01",  # falsches Suffix
        b"\x02" + b"\xff" * 36 + b"\x00",  # kein druckbares ASCII
    ],
)
def test_non_references_are_rejected(blob: bytes | None) -> None:
    assert not is_external_reference(blob)


def test_inline_content_of_reference_length_is_not_mistaken() -> None:
    """Ein 38 Byte langer Inhalt darf nicht als Referenz gelten."""
    assert not is_external_reference(b"\xff\xd8\xff\xe0" + b"x" * 34)


# ---------------------------------------------------------------------------
# Richtungserkennung
# ---------------------------------------------------------------------------


def test_orphaned_direction_is_detected_and_reported(threema_backup: ThreemaBackup) -> None:
    """Der Kernfall: ZIMAGEDATA.ZMESSAGE traegt nicht, wird aber erkannt."""
    result = _enumerate(threema_backup)
    assert result.is_supported
    assert any("ZIMAGEDATA.ZMESSAGE ist vollstaendig verwaist" in n for n in result.notes)


def test_message_side_direction_is_used(threema_backup: ThreemaBackup) -> None:
    """Trotz verwaister Rueckrichtung werden Bilder zugeordnet."""
    result = _enumerate(threema_backup)
    evidence = {item.evidence for item in result.items if item.evidence}
    assert "ZMESSAGE.ZTHUMBNAIL -> ZIMAGEDATA.Z_PK" in evidence
    assert "ZMESSAGE.ZIMAGE -> ZIMAGEDATA.Z_PK" in evidence
    assert "ZMESSAGE.ZVIDEO -> ZVIDEODATA.Z_PK" in evidence
    assert "ZMESSAGE.ZDATA -> ZFILEDATA.Z_PK" in evidence


# ---------------------------------------------------------------------------
# Quellen
# ---------------------------------------------------------------------------


def test_both_source_kinds_are_found(threema_backup: ThreemaBackup) -> None:
    """Externe Dateien UND Inline-Blobs - sonst gehen Medien verloren."""
    result = _enumerate(threema_backup)
    kinds = {item.source.kind for item in result.items}
    assert kinds == {SourceKind.EXTERNAL_FILE, SourceKind.INLINE_BLOB}


def test_external_items_resolve_to_backup_files(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    for item in result.items:
        if item.source.is_external:
            assert item.source.file_id
            assert (threema_backup.path / item.source.file_id[:2] / item.source.file_id).is_file()


def test_inline_items_carry_table_address(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    inline = [i for i in result.items if not i.source.is_external]
    assert inline
    for item in inline:
        assert item.source.table in {"ZFILEDATA", "ZIMAGEDATA", "ZVIDEODATA", "ZAUDIODATA"}
        assert item.source.row_id is not None
        assert item.source.column == "ZDATA"


def test_dangling_reference_is_counted_not_fatal(threema_backup: ThreemaBackup) -> None:
    """Das Fixture verweist absichtlich auf eine fehlende Datei."""
    result = _enumerate(threema_backup)
    assert result.dangling_references == 1
    assert any("nicht vorhanden" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Chat-Zuordnung
# ---------------------------------------------------------------------------


def test_all_chat_naming_fallbacks_work(threema_backup: ThreemaBackup) -> None:
    """Gruppenname, Vor-/Nachname, Nickname, Threema-ID - in dieser Reihenfolge."""
    result = _enumerate(threema_backup)
    names = {item.chat.display_name for item in result.items if item.chat}
    assert set(EXPECTED_CHAT_NAMES.values()) <= names


def test_group_and_direct_chats_are_distinguished(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    kinds = {item.chat.name: item.chat.kind for item in result.items if item.chat}
    assert kinds["Familie"] == "group"
    assert kinds["Max Mustermann"] == "direct"


def test_media_without_message_stays_unassigned(threema_backup: ThreemaBackup) -> None:
    """Ohne belegbare Verknuepfung wird nicht geraten."""
    result = _enumerate(threema_backup)
    unassigned = [item for item in result.items if not item.is_assigned]
    assert len(unassigned) == 1
    assert unassigned[0].evidence is None
    assert unassigned[0].original_filename is None


def test_every_assignment_carries_evidence(threema_backup: ThreemaBackup) -> None:
    """Eine Zuordnung ohne Beleg waere geraten."""
    result = _enumerate(threema_backup)
    for item in result.items:
        if item.is_assigned:
            assert item.evidence, f"{item.source.identity()} ohne Beleg"


# ---------------------------------------------------------------------------
# Metadaten
# ---------------------------------------------------------------------------


def test_original_filenames_are_taken_from_the_database(
    threema_backup: ThreemaBackup,
) -> None:
    result = _enumerate(threema_backup)
    names = {item.original_filename for item in result.items if item.original_filename}
    assert {"urlaub.jpg", "Vertrag.pdf", "clip.mp4", "sprachnachricht.m4a", "foto.png"} <= names


def test_missing_filename_stays_none(threema_backup: ThreemaBackup) -> None:
    """Ein fehlender Name wird nicht erfunden."""
    result = _enumerate(threema_backup)
    assert any(
        item.original_filename is None and item.is_assigned for item in result.items
    )


def test_timestamps_are_converted_from_apple_epoch(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    stamps = {item.timestamp for item in result.items if item.timestamp}
    assert stamps
    for stamp in stamps:
        assert stamp.year == 2025
        assert stamp.month == 3
        assert stamp.day == 14


def test_missing_timestamp_stays_none(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    assert any(
        item.timestamp is None and item.original_filename == "ohne-datum.pdf"
        for item in result.items
    )


def test_declared_mime_is_carried_but_not_authoritative(
    threema_backup: ThreemaBackup,
) -> None:
    result = _enumerate(threema_backup)
    mimes = {item.declared_mime for item in result.items if item.declared_mime}
    assert {"image/jpeg", "application/pdf", "video/mp4", "audio/mp4"} <= mimes


# ---------------------------------------------------------------------------
# Vorschaubilder
# ---------------------------------------------------------------------------


def test_thumbnails_are_marked_and_linked(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    thumbnails = [item for item in result.items if item.is_thumbnail]
    assert len(thumbnails) == 1
    thumbnail = thumbnails[0]
    assert thumbnail.thumbnail_of is not None
    originals = {
        item.source.identity() for item in result.items if not item.is_thumbnail
    }
    assert thumbnail.thumbnail_of in originals


def test_originals_are_not_marked_as_thumbnails(threema_backup: ThreemaBackup) -> None:
    result = _enumerate(threema_backup)
    for item in result.items:
        if item.original_filename == "Vertrag.pdf":
            assert not item.is_thumbnail


# ---------------------------------------------------------------------------
# Nicht unterstuetztes Schema
# ---------------------------------------------------------------------------


def test_missing_tables_yield_unsupported_not_a_guess(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "fremd.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE Irgendwas (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with open_readonly(path) as ro:
        context = MediaContext(
            connection=ro,
            schemas=describe_database(ro, count_rows=False),
            external_files={},
            entries_by_path={},
        )
        result = ThreemaProfile().enumerate_media(context)
    assert not result.is_supported
    assert result.items == ()
    assert "ZMESSAGE" in (result.unsupported_reason or "")


def test_profile_declares_required_tables() -> None:
    assert ThreemaProfile().requires_tables() == ("ZMESSAGE", "ZCONVERSATION")


# ---------------------------------------------------------------------------
# Core-Data-Markierungsbyte
# ---------------------------------------------------------------------------


def test_inline_blobs_declare_the_prefix_offset(threema_backup: ThreemaBackup) -> None:
    """Core Data stellt Inline-Daten ein 0x01 voran; das muss abgeschnitten werden.

    Ohne dieses Abschneiden waere jede aus der Datenbank exportierte Datei um
    ein Byte verschoben - ein JPEG liesse sich nicht oeffnen. Am echten Backup
    betraf das 5.973 Blobs, davon 5.943 JPEGs.
    """
    result = _enumerate(threema_backup)
    inline = [item for item in result.items if not item.source.is_external]
    assert inline
    for item in inline:
        assert item.source.byte_offset == 1, "Der Vorspann wird nicht abgeschnitten"


def test_inline_size_excludes_the_prefix(threema_backup: ThreemaBackup) -> None:
    """Die gemeldete Groesse darf das Markierungsbyte nicht mitzaehlen."""
    result = _enumerate(threema_backup)
    for item in result.items:
        if item.source.is_external:
            continue
        expected = threema_backup.fixture.by_identity(
            f"{item.source.table}:{item.source.row_id}:{item.source.column}"
        )
        assert item.size == len(expected.content)


def test_inline_content_is_recognised_by_magic_bytes(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Nach dem Abschneiden greift die Signaturerkennung wieder."""
    from msgbackup_extractor.core.backup import AppleBackup
    from msgbackup_extractor.core.session import BackupSession
    from msgbackup_extractor.core.sqlite_ro import open_readonly as _open
    from msgbackup_extractor.extract.sources import MediaReader
    from msgbackup_extractor.models import DetectionMethod

    result = _enumerate(threema_backup)
    inline = [item for item in result.items if not item.source.is_external]
    assert inline

    with ManifestReader(threema_backup.path / "Manifest.db") as reader:
        entries = {e.file_id: e for e in reader.entries() if e.kind is FileKind.FILE}
        by_path = {e.relative_path: e for e in entries.values()}
    db_entry = by_path[DATABASE_NAME]
    db_path = threema_backup.path / db_entry.file_id[:2] / db_entry.file_id

    with BackupSession(AppleBackup(threema_backup.path)) as session, _open(db_path) as con:
        media_reader = MediaReader(
            backup=session.backup, connection=con, entries=entries
        )
        for item in inline:
            detected = media_reader.detect_type(item)
            assert detected.detection_method is DetectionMethod.MAGIC, (
                f"{item.source.identity()} wurde nicht per Signatur erkannt"
            )
            assert detected.format_name in {"JPEG", "M4A", "PNG", "PDF", "MP4"}


def test_unexpected_prefix_is_reported_not_stripped(tmp_path: Path) -> None:
    """Ein unerwartetes erstes Byte wird gemeldet, nicht blind abgeschnitten.

    Falsch abzuschneiden waere schlimmer als nicht abzuschneiden, weil es
    unbemerkt bliebe.
    """
    import sqlite3

    from tests.support.backup_builder import BackupFile, build_backup
    from tests.support.threema_fixture import (
        GROUP_DOMAIN,
        JPEG,
        build_threema_store,
    )

    fixture = build_threema_store()
    # Ein Blob mit falscher Markierung in den Store schmuggeln.
    db_path = tmp_path / "store.sqlite"
    db_path.write_bytes(fixture.database)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO ZFILEDATA (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, ZDATA) "
        "VALUES (9999, 6, 1, NULL, ?)",
        (b"\x07" + JPEG + b"x" * 50,),
    )
    connection.commit()
    connection.close()

    files = [
        f for f in fixture.backup_files() if f.relative_path != DATABASE_NAME
    ] + [BackupFile(GROUP_DOMAIN, DATABASE_NAME, db_path.read_bytes())]
    backup = build_backup(
        tmp_path / "b", files, installed_applications=["ch.threema.iapp"]
    )
    target = ThreemaBackup(backup=backup, fixture=fixture)

    result = _enumerate(target)
    assert any("Markierungsbyte" in note for note in result.notes)
    smuggled = [
        item
        for item in result.items
        if item.source.table == "ZFILEDATA" and item.source.row_id == 9999
    ]
    assert smuggled and smuggled[0].source.byte_offset == 0
