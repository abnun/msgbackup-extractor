"""Tests fuer das WhatsApp-Profil.

WhatsApp speichert Medien als Dateien und nennt in der Datenbank Pfade, denen
das Praefix `Message/` fehlt. Genau daran entscheidet sich, ob die Zuordnung
funktioniert - deshalb bildet das Fixture das nach.
"""

from __future__ import annotations

from pathlib import Path

from msgbackup_extractor.apps.base import MediaContext
from msgbackup_extractor.apps.whatsapp import WhatsAppProfile
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.hashing import hash_bytes, hash_file
from msgbackup_extractor.core.manifest import ManifestReader
from msgbackup_extractor.core.session import BackupSession
from msgbackup_extractor.core.sqlite_ro import describe_database, open_readonly
from msgbackup_extractor.extraction import Extractor
from msgbackup_extractor.models import DetectionStatus, FileKind, SourceKind
from tests.conftest import WHATSAPP_BUNDLE_ID, WhatsAppBackup
from tests.support.whatsapp_fixture import DATABASE_NAME, PATH_PREFIX


def _enumerate(target: WhatsAppBackup):
    with ManifestReader(target.path / "Manifest.db") as reader:
        entries = [e for e in reader.entries() if e.kind is FileKind.FILE]
    by_path = {e.relative_path: e for e in entries}
    db_entry = by_path[DATABASE_NAME]
    db_path = target.path / db_entry.file_id[:2] / db_entry.file_id
    with open_readonly(db_path) as connection:
        context = MediaContext(
            connection=connection,
            schemas=describe_database(connection, count_rows=False),
            external_files={},
            entries_by_path=by_path,
        )
        return WhatsAppProfile().enumerate_media(context)


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------


def test_whatsapp_is_detected(whatsapp_backup: WhatsAppBackup) -> None:
    result = WhatsAppProfile().detect(AppleBackup(whatsapp_backup.path).info())
    assert result.status is DetectionStatus.CONFIRMED
    assert result.bundle_id == WHATSAPP_BUNDLE_ID
    assert result.bundle_version == "1041553870.0"


def test_domains_include_the_shared_group(whatsapp_backup: WhatsAppBackup) -> None:
    with ManifestReader(whatsapp_backup.path / "Manifest.db") as reader:
        domains = WhatsAppProfile().match_domains(
            WHATSAPP_BUNDLE_ID, reader.domain_names()
        )
    kinds = {d.kind for d in domains}
    assert "group" in kinds and "app" in kinds
    assert any("WhatsApp.shared" in d.domain for d in domains)


def test_requires_the_measured_tables() -> None:
    assert WhatsAppProfile().requires_tables() == (
        "ZWAMESSAGE", "ZWACHATSESSION", "ZWAMEDIAITEM"
    )


# ---------------------------------------------------------------------------
# Pfadpraefix
# ---------------------------------------------------------------------------


def test_path_prefix_is_measured_not_assumed(whatsapp_backup: WhatsAppBackup) -> None:
    """Ein fest verdrahtetes Praefix wuerde bei einer Umstellung stumm scheitern."""
    result = _enumerate(whatsapp_backup)
    assert result.is_supported
    assert any("Praefix der Medienpfade bestimmt" in n for n in result.notes)
    assert any(PATH_PREFIX in n for n in result.notes)


def test_all_media_resolve_to_backup_files(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    assert result.items
    for item in result.items:
        assert item.source.kind is SourceKind.EXTERNAL_FILE
        assert item.source.file_id
        assert (
            whatsapp_backup.path / item.source.file_id[:2] / item.source.file_id
        ).is_file()


def test_unresolvable_path_is_counted_not_fatal(whatsapp_backup: WhatsAppBackup) -> None:
    """Das Fixture verweist auf eine Datei, die es nicht gibt."""
    result = _enumerate(whatsapp_backup)
    assert result.dangling_references == 1
    assert any("nicht vorhanden" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Beziehungsrichtung
# ---------------------------------------------------------------------------


def test_relationship_direction_is_measured(whatsapp_backup: WhatsAppBackup) -> None:
    """Bei WhatsApp tragen beide Richtungen - gemessen wird trotzdem."""
    result = _enumerate(whatsapp_backup)
    assert any("Beziehung gemessen" in n for n in result.notes)
    evidence = {item.evidence for item in result.items if item.evidence}
    assert evidence
    assert all("ZWAMEDIAITEM" in e or "ZWAMESSAGE" in e for e in evidence)


# ---------------------------------------------------------------------------
# Chat-Zuordnung und Metadaten
# ---------------------------------------------------------------------------


def test_chats_are_named_from_the_database(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    names = {item.chat.display_name for item in result.items if item.chat}
    assert {"Wanderfreunde", "Erika Beispiel"} <= names


def test_group_and_direct_chats_are_distinguished(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    kinds = {item.chat.name: item.chat.kind for item in result.items if item.chat}
    assert kinds["Wanderfreunde"] == "group"
    assert kinds["Erika Beispiel"] == "direct"


def test_chat_without_a_name_falls_back_to_its_id(whatsapp_backup: WhatsAppBackup) -> None:
    """Die Kontakt-JID waere eine Telefonnummer und taugt nicht als Name."""
    result = _enumerate(whatsapp_backup)
    unnamed = [item for item in result.items if item.chat and item.chat.name is None]
    assert unnamed
    assert unnamed[0].chat.display_name.startswith("chat-")


def test_timestamps_use_the_apple_epoch(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    stamps = [item.timestamp for item in result.items if item.timestamp]
    assert stamps
    assert all(s.year == 2024 and s.month == 6 for s in stamps)


def test_missing_timestamp_stays_none(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    assert any(item.timestamp is None for item in result.items)


def test_title_is_used_only_when_it_looks_like_a_filename(
    whatsapp_backup: WhatsAppBackup,
) -> None:
    """`ZTITLE` traegt bei Dokumenten den Namen, sonst Beschreibungen.

    Eine Beschreibung als Dateinamen zu nehmen waere ein erfundener Name.
    """
    result = _enumerate(whatsapp_backup)
    names = {item.original_filename for item in result.items if item.original_filename}
    assert "Rechnung 2024.pdf" in names
    assert "Ein Ort ohne Endung" not in names


def test_thumbnails_are_marked_and_linked(whatsapp_backup: WhatsAppBackup) -> None:
    result = _enumerate(whatsapp_backup)
    thumbnails = [item for item in result.items if item.is_thumbnail]
    assert thumbnails
    originals = {i.source.identity() for i in result.items if not i.is_thumbnail}
    for thumbnail in thumbnails:
        assert thumbnail.thumbnail_of in originals


# ---------------------------------------------------------------------------
# Nicht unterstuetztes Schema
# ---------------------------------------------------------------------------


def test_missing_tables_yield_unsupported(tmp_path: Path) -> None:
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
        result = WhatsAppProfile().enumerate_media(context)
    assert not result.is_supported
    assert "ZWAMESSAGE" in (result.unsupported_reason or "")


def test_unresolvable_prefix_yields_unsupported(
    whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> None:
    """Loest kein Praefix die Pfade auf, wird nichts zugeordnet - nicht geraten."""
    with ManifestReader(whatsapp_backup.path / "Manifest.db") as reader:
        entries = [e for e in reader.entries() if e.kind is FileKind.FILE]
    by_path = {e.relative_path: e for e in entries}
    db_entry = by_path[DATABASE_NAME]
    db_path = whatsapp_backup.path / db_entry.file_id[:2] / db_entry.file_id
    with open_readonly(db_path) as connection:
        # Nur die Datenbank selbst als bekannte Datei: kein Medienpfad passt.
        context = MediaContext(
            connection=connection,
            schemas=describe_database(connection, count_rows=False),
            external_files={},
            entries_by_path={DATABASE_NAME: db_entry},
        )
        result = WhatsAppProfile().enumerate_media(context)
    assert not result.is_supported
    assert "geraten" in (result.unsupported_reason or "")


# ---------------------------------------------------------------------------
# Ende-zu-Ende
# ---------------------------------------------------------------------------


def test_extraction_recovers_every_expected_file(
    whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        outcome = Extractor(session=session, output_dir=output, app_slug="whatsapp").run()

    assert outcome.result.integrity_errors == 0
    assert outcome.result.successful > 0
    on_disk = {hash_file(p) for p in output.rglob("*") if p.is_file()}
    for expected in whatsapp_backup.fixture.expected:
        assert hash_bytes(expected.content) in on_disk, expected.relative_path


def test_extraction_creates_chat_structure(
    whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        Extractor(session=session, output_dir=output, app_slug="whatsapp").run()
    chats = {p.name for p in (output / "chats").iterdir() if p.is_dir()}
    assert {"Wanderfreunde", "Erika Beispiel"} <= chats


def test_orphan_file_is_still_exported(
    whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> None:
    """Eine Datei, die die Datenbank nicht kennt, darf nicht verloren gehen."""
    output = tmp_path / "export"
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        Extractor(session=session, output_dir=output, app_slug="whatsapp").run()
    orphan = whatsapp_backup.fixture.media[PATH_PREFIX + "Media/gruppe-a/1/3/waise.jpg"]
    on_disk = {hash_file(p) for p in output.rglob("*") if p.is_file()}
    assert hash_bytes(orphan) in on_disk


def test_phone_numbers_do_not_reach_the_manifest(
    whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> None:
    """Die Medienpfade enthalten Telefonnummern - das Manifest darf sie nicht tragen."""
    from msgbackup_extractor.extract import export_manifest

    output = tmp_path / "export"
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        outcome = Extractor(session=session, output_dir=output, app_slug="whatsapp").run()
    payload = export_manifest.build(
        outcome.result, app="whatsapp", backup_udid="T", tool_version="0"
    )
    text = export_manifest.write(payload, output).read_text(encoding="utf-8")
    assert "@s.example" not in text
    assert "Media/kontakt-b" not in text
