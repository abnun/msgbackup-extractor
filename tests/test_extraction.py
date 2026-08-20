"""Tests fuer Planung und Ausfuehrung der Extraktion."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from msgbackup_extractor.core.hashing import hash_bytes, hash_file
from msgbackup_extractor.extract.planner import ExtractOptions
from msgbackup_extractor.extraction import ExtractionBlocked
from msgbackup_extractor.models import FileOutcome, MediaCategory, SourceKind
from tests.conftest import TEST_PASSWORD, ThreemaBackup, extract


def _files_under(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    }


# ---------------------------------------------------------------------------
# Probelauf
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(threema_backup: ThreemaBackup, tmp_path: Path) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output, options=ExtractOptions(dry_run=True))
    assert outcome.plan.total_files > 0
    assert not output.exists() or _files_under(output) == set()


def test_dry_run_plan_matches_real_run(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Probelauf und echter Lauf beruhen auf demselben Plan."""
    dry = extract(threema_backup, tmp_path / "a", options=ExtractOptions(dry_run=True))
    real = extract(threema_backup, tmp_path / "b")
    assert dry.plan.total_files == real.plan.total_files
    assert {str(f.output_path) for f in dry.plan.files} == {
        str(f.output_path) for f in real.plan.files
    }


def test_dry_run_reports_categories(threema_backup: ThreemaBackup, tmp_path: Path) -> None:
    outcome = extract(threema_backup, tmp_path / "e", options=ExtractOptions(dry_run=True))
    counts = outcome.plan.counts_per_category()
    assert counts.get(MediaCategory.IMAGE.value, 0) >= 2
    assert counts.get(MediaCategory.VIDEO.value, 0) >= 1
    assert counts.get(MediaCategory.DOCUMENT.value, 0) >= 1


# ---------------------------------------------------------------------------
# Echter Lauf: Inhalte
# ---------------------------------------------------------------------------


def test_every_expected_medium_is_extracted_byte_for_byte(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Der zentrale Test: der Inhalt jeder Datei stimmt exakt."""
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)

    by_hash: dict[str, Path] = {}
    for path in output.rglob("*"):
        if path.is_file():
            by_hash.setdefault(hash_file(path), path)

    checked = 0
    for expected in threema_backup.fixture.expected:
        digest = hash_bytes(expected.content)
        assert digest in by_hash, f"{expected.identity} fehlt im Export"
        checked += 1
    assert checked == len(threema_backup.fixture.expected)
    assert outcome.result.integrity_errors == 0


def test_inline_blobs_are_extracted(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Ohne diesen Pfad gingen Medien verloren, die nur in der DB liegen."""
    outcome = extract(threema_backup, tmp_path / "export")
    inline = [
        f for f in outcome.result.files if f.source_kind is SourceKind.INLINE_BLOB
    ]
    assert inline
    assert all(f.outcome is FileOutcome.EXTRACTED for f in inline)
    for entry in inline:
        assert entry.source_table
        assert entry.source_row_id is not None


def test_external_files_are_extracted(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    outcome = extract(threema_backup, tmp_path / "export")
    external = [
        f
        for f in outcome.result.files
        if f.source_kind is SourceKind.EXTERNAL_FILE
        and f.outcome is FileOutcome.EXTRACTED
    ]
    assert len(external) >= 6


def test_integrity_is_verified_for_every_file(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    outcome = extract(threema_backup, tmp_path / "export")
    extracted = [f for f in outcome.result.files if f.outcome is FileOutcome.EXTRACTED]
    assert extracted
    for entry in extracted:
        assert entry.integrity_ok is True
        assert entry.sha256 and len(entry.sha256) == 64


def test_manifest_hashes_match_the_written_files(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    for entry in outcome.result.files:
        if entry.outcome is not FileOutcome.EXTRACTED or entry.output_path is None:
            continue
        assert hash_file(output / entry.output_path) == entry.sha256


# ---------------------------------------------------------------------------
# Struktur
# ---------------------------------------------------------------------------


def test_media_are_sorted_by_type(threema_backup: ThreemaBackup, tmp_path: Path) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    assert (output / "media" / "images").is_dir()
    assert (output / "media" / "videos").is_dir()
    assert (output / "media" / "documents").is_dir()


def test_chat_structure_is_created(threema_backup: ThreemaBackup, tmp_path: Path) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    chats = output / "chats"
    assert chats.is_dir()
    present = {p.name for p in chats.iterdir() if p.is_dir()}
    assert {"Familie", "Max Mustermann", "nordlicht"} <= present


def test_unassignable_media_go_to_unassigned(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    unassigned = output / "chats" / "unassigned"
    assert unassigned.is_dir()
    assert list(unassigned.rglob("*")), "unassigned/ sollte die eine Datei enthalten"


def test_chat_paths_are_hardlinks_not_copies(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Hardlinks kosten keinen zusaetzlichen Speicher."""
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    linked = [f for f in outcome.result.files if f.link_paths and f.output_path]
    assert linked
    for entry in linked:
        primary = (output / entry.output_path).stat()
        for link in entry.link_paths:
            secondary = (output / link).stat()
            assert secondary.st_ino == primary.st_ino, "keine Hardlink-Verknuepfung"


def test_copies_are_used_when_hardlinks_are_disabled(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output, options=ExtractOptions(hardlinks=False))
    linked = [f for f in outcome.result.files if f.link_paths and f.output_path]
    assert linked
    entry = linked[0]
    primary = output / entry.output_path
    secondary = output / entry.link_paths[0]
    assert primary.stat().st_ino != secondary.stat().st_ino
    assert primary.read_bytes() == secondary.read_bytes()


def test_chat_structure_can_be_disabled(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output, options=ExtractOptions(organize_by_chat=False))
    assert not (output / "chats").exists()
    assert (output / "media").is_dir()


def test_app_internals_go_to_metadata(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Plists und Logs duerfen nicht zwischen den Fotos landen."""
    output = tmp_path / "export"
    extract(threema_backup, output)
    metadata = output / "metadata"
    assert metadata.is_dir()
    names = {p.name for p in metadata.iterdir() if p.is_file()}
    assert any(name.endswith(".plist") for name in names)
    images = {p.name for p in (output / "media" / "images").iterdir()}
    assert not any(name.endswith(".plist") for name in images)


def test_databases_go_to_their_own_directory(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    databases = output / "databases"
    assert databases.is_dir()
    assert any("ThreemaData" in p.name for p in databases.iterdir())


# ---------------------------------------------------------------------------
# Vorschaubilder
# ---------------------------------------------------------------------------


def test_thumbnails_are_exported_separately(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    assert (output / "media" / "thumbnails").is_dir()
    thumbnails = [f for f in outcome.result.files if f.is_thumbnail]
    assert thumbnails
    for entry in thumbnails:
        assert entry.output_path and entry.output_path.startswith("media/thumbnails/")
        assert entry.thumbnail_of, "Ein UI braucht die Zuordnung zum Original"


def test_thumbnails_can_be_excluded(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(
        threema_backup, output, options=ExtractOptions(include_thumbnails=False)
    )
    assert not (output / "media" / "thumbnails").exists()
    assert not any(f.is_thumbnail for f in outcome.result.files)
    assert any("Vorschaubild" in reason for _, reason in outcome.plan.excluded)


# ---------------------------------------------------------------------------
# Dateinamen
# ---------------------------------------------------------------------------


def test_original_filenames_are_preferred(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    names = _files_under(output)
    assert any(name.endswith("Vertrag.pdf") for name in names)
    assert any(name.endswith("clip.mp4") for name in names)


def test_fallback_name_uses_timestamp_and_type(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    generated = [
        Path(f.output_path).name
        for f in outcome.result.files
        if f.output_path and f.original_filename is None and f.timestamp is not None
    ]
    assert generated
    assert any(name.startswith("2025-03-14_18-42-11_") for name in generated)


def test_unknown_date_prefix_when_no_timestamp(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    generated = [
        Path(f.output_path).name
        for f in outcome.result.files
        if f.output_path and f.original_filename is None and f.timestamp is None
    ]
    assert generated
    assert all(name.startswith("unknown-date_") for name in generated)


def test_filenames_are_unique(threema_backup: ThreemaBackup, tmp_path: Path) -> None:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    paths = [f.output_path for f in outcome.result.files if f.output_path]
    assert len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# Filter und Duplikate
# ---------------------------------------------------------------------------


def test_category_filter_limits_the_export(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(
        threema_backup,
        output,
        options=ExtractOptions(categories=frozenset({MediaCategory.VIDEO})),
    )
    categories = {f.media_type for f in outcome.result.files if f.media_type}
    assert categories and all(mime.startswith("video/") for mime in categories)
    assert not (output / "media" / "images").exists()


def test_duplicates_are_marked_by_default(tmp_path: Path) -> None:
    """Duplikate werden markiert, nicht automatisch geloescht."""
    from tests.support.backup_builder import BackupFile, build_backup
    from tests.support.threema_fixture import GROUP_DOMAIN, JPEG, build_threema_store

    fixture = build_threema_store()
    files = fixture.backup_files()
    identical = JPEG + b"Z" * 100
    files.append(BackupFile(GROUP_DOMAIN, "Documents/kopie-a.jpg", identical))
    files.append(BackupFile(GROUP_DOMAIN, "Documents/kopie-b.jpg", identical))
    backup = build_backup(
        tmp_path / "b", files, installed_applications=["ch.threema.iapp"]
    )

    from tests.conftest import ThreemaBackup

    target = ThreemaBackup(backup=backup, fixture=fixture)
    output = tmp_path / "export"
    outcome = extract(target, output)
    duplicates = [
        f for f in outcome.result.files if f.duplicate_of and f.outcome is FileOutcome.EXTRACTED
    ]
    assert duplicates, "Ein Duplikat sollte markiert sein"
    for entry in duplicates:
        assert entry.output_path and (output / entry.output_path).is_file()


def test_deduplicate_writes_content_only_once(tmp_path: Path) -> None:
    from tests.conftest import ThreemaBackup
    from tests.support.backup_builder import BackupFile, build_backup
    from tests.support.threema_fixture import GROUP_DOMAIN, JPEG, build_threema_store

    fixture = build_threema_store()
    files = fixture.backup_files()
    identical = JPEG + b"Y" * 120
    files.append(BackupFile(GROUP_DOMAIN, "Documents/kopie-a.jpg", identical))
    files.append(BackupFile(GROUP_DOMAIN, "Documents/kopie-b.jpg", identical))
    backup = build_backup(
        tmp_path / "b", files, installed_applications=["ch.threema.iapp"]
    )
    target = ThreemaBackup(backup=backup, fixture=fixture)

    output = tmp_path / "export"
    outcome = extract(target, output, options=ExtractOptions(deduplicate=True))
    duplicates = [f for f in outcome.result.files if f.outcome is FileOutcome.DUPLICATE]
    assert duplicates
    digest = hash_bytes(identical)
    on_disk = [p for p in output.rglob("*") if p.is_file() and hash_file(p) == digest]
    inodes = {p.stat().st_ino for p in on_disk}
    assert len(inodes) == 1, "Der Inhalt darf nur einmal auf der Platte liegen"


# ---------------------------------------------------------------------------
# Fehlertoleranz
# ---------------------------------------------------------------------------


def test_missing_source_is_reported_and_run_continues(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Das Fixture verweist auf eine Datei, die es nicht gibt."""
    outcome = extract(threema_backup, tmp_path / "export")
    assert outcome.dangling_references == 1
    assert outcome.result.successful > 0


def test_unreadable_payload_does_not_abort_the_run(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    """Eine einzelne unlesbare Datei kostet nicht den gesamten Export."""
    from tests.support.threema_fixture import EXTERNAL_DIR

    victim = next(
        f
        for f in threema_backup.backup.files
        if f.relative_path.startswith(f"{EXTERNAL_DIR}/")
    )
    path = threema_backup.path / victim.file_id[:2] / victim.file_id
    path.chmod(0o000)
    try:
        outcome = extract(threema_backup, tmp_path / "export")
    finally:
        path.chmod(0o644)
    assert outcome.result.successful > 0
    assert outcome.result.failed >= 1
    assert any(
        f.outcome is FileOutcome.MISSING or f.error for f in outcome.result.files
    )


def test_partial_file_is_removed_on_failure(
    threema_backup: ThreemaBackup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein Abbruch mitten im Schreiben darf keine Teildatei hinterlassen."""
    from msgbackup_extractor.extract import sources

    original = sources.MediaReader.stream
    calls = {"n": 0}

    def failing(self, item):
        calls["n"] += 1
        if calls["n"] == 2:
            yield b"teil"
            raise OSError("simulierter Abbruch")
        yield from original(self, item)

    monkeypatch.setattr(sources.MediaReader, "stream", failing)
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    assert outcome.result.failed >= 1
    for path in output.rglob("*"):
        if path.is_file():
            assert path.read_bytes() != b"teil", "Teildatei wurde nicht entfernt"


# ---------------------------------------------------------------------------
# Ausgabeverzeichnis
# ---------------------------------------------------------------------------


def test_output_inside_the_backup_is_refused(
    threema_backup: ThreemaBackup,
) -> None:
    from msgbackup_extractor.core.paths import OutputGuardError

    with pytest.raises(OutputGuardError, match="innerhalb des Backups"):
        extract(threema_backup, threema_backup.path / "export")


def test_nothing_is_written_outside_the_output_directory(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    sibling = tmp_path / "daneben"
    sibling.mkdir()
    extract(threema_backup, output)
    assert list(sibling.iterdir()) == []


# ---------------------------------------------------------------------------
# Verschluesselte Backups
# ---------------------------------------------------------------------------


def test_extraction_from_encrypted_backup(
    encrypted_threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    outcome = extract(encrypted_threema_backup, output, password=TEST_PASSWORD)
    assert outcome.result.integrity_errors == 0
    assert outcome.result.successful > 0

    by_hash = {hash_file(p) for p in output.rglob("*") if p.is_file()}
    for expected in encrypted_threema_backup.fixture.expected:
        assert hash_bytes(expected.content) in by_hash, expected.identity


def test_encrypted_and_plain_extraction_agree(
    threema_backup: ThreemaBackup,
    encrypted_threema_backup: ThreemaBackup,
    tmp_path: Path,
) -> None:
    """Verschluesselt und unverschluesselt muessen dasselbe Ergebnis liefern."""
    plain = extract(threema_backup, tmp_path / "plain")
    encrypted = extract(
        encrypted_threema_backup, tmp_path / "enc", password=TEST_PASSWORD
    )
    assert plain.result.successful == encrypted.result.successful
    plain_hashes = {f.sha256 for f in plain.result.files if f.sha256}
    encrypted_hashes = {f.sha256 for f in encrypted.result.files if f.sha256}
    assert plain_hashes == encrypted_hashes


def test_encrypted_backup_without_password_is_blocked(
    encrypted_threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    with pytest.raises(ExtractionBlocked, match="verschluesselt"):
        extract(encrypted_threema_backup, tmp_path / "export")


# ---------------------------------------------------------------------------
# Read-only-Zusage
# ---------------------------------------------------------------------------


def test_extraction_never_modifies_the_backup(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    from tests.test_readonly_guarantee import assert_unchanged, fingerprint

    before = fingerprint(threema_backup.path)
    outcome = extract(threema_backup, tmp_path / "export")
    assert outcome.result.successful > 0
    assert_unchanged(threema_backup.path, before)


def test_extraction_from_encrypted_backup_never_modifies_it(
    encrypted_threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    from tests.test_readonly_guarantee import assert_unchanged, fingerprint

    before = fingerprint(encrypted_threema_backup.path)
    extract(encrypted_threema_backup, tmp_path / "export", password=TEST_PASSWORD)
    assert_unchanged(encrypted_threema_backup.path, before)


def test_no_app_data_lands_outside_expected_directories(
    threema_backup: ThreemaBackup, tmp_path: Path
) -> None:
    output = tmp_path / "export"
    extract(threema_backup, output)
    allowed = {"media", "chats", "databases", "metadata", "reports"}
    for path in output.iterdir():
        if path.is_dir():
            assert path.name in allowed, f"Unerwartetes Verzeichnis {path.name}"


# ---------------------------------------------------------------------------
# Blockade-Faelle
# ---------------------------------------------------------------------------


def test_backup_without_the_app_is_blocked(
    backup_without_threema, tmp_path: Path
) -> None:
    from msgbackup_extractor.core.backup import AppleBackup
    from msgbackup_extractor.core.session import BackupSession
    from msgbackup_extractor.extraction import Extractor

    with (
        BackupSession(AppleBackup(backup_without_threema.path)) as session,
        pytest.raises(ExtractionBlocked, match="kein unterstuetzter Messenger"),
    ):
        Extractor(session=session, output_dir=tmp_path / "export").run()


def test_hardlink_across_filesystems_falls_back_to_copy(
    threema_backup: ThreemaBackup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hardlinks gehen nicht ueberall - dann muss kopiert werden."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", refuse)
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    linked = [f for f in outcome.result.files if f.link_paths and f.output_path]
    assert linked
    entry = linked[0]
    assert (output / entry.link_paths[0]).is_file()
    assert (output / entry.link_paths[0]).read_bytes() == (
        output / entry.output_path
    ).read_bytes()
