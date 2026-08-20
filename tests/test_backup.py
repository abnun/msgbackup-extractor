"""Tests fuer den read-only Zugriff auf das Backup-Verzeichnis."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from msgbackup_extractor.core.backup import (
    AppleBackup,
    BackupAccessError,
    NotABackupError,
    default_backup_root,
    list_local_backups,
)
from tests.conftest import THREEMA_BUNDLE_ID, sample_files
from tests.support.backup_builder import BackupFile, BuiltBackup, build_backup

# ---------------------------------------------------------------------------
# Erkennung eines Backups
# ---------------------------------------------------------------------------


def test_opens_valid_backup(plain_backup: BuiltBackup) -> None:
    backup = AppleBackup(plain_backup.path)
    assert backup.udid == plain_backup.udid
    assert not backup.is_encrypted


def test_rejects_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "datei.txt"
    target.write_text("kein backup")
    with pytest.raises(NotABackupError, match="Kein Verzeichnis"):
        AppleBackup(target)


def test_rejects_directory_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "leer").mkdir()
    with pytest.raises(NotABackupError, match=re.escape("Manifest.plist")):
        AppleBackup(tmp_path / "leer")


def test_error_names_the_missing_files(tmp_path: Path) -> None:
    directory = tmp_path / "halb"
    directory.mkdir()
    (directory / "Manifest.plist").write_bytes(plistlib.dumps({}))
    with pytest.raises(NotABackupError) as error:
        AppleBackup(directory)
    assert "Manifest.db" in str(error.value)
    assert "Manifest.plist" not in str(error.value).split("fehlen:")[1]


def test_default_backup_root_points_at_mobilesync() -> None:
    root = default_backup_root(Path("/Users/test"))
    assert root == Path("/Users/test/Library/Application Support/MobileSync/Backup")


# ---------------------------------------------------------------------------
# Metadaten
# ---------------------------------------------------------------------------


def test_device_info_from_plists(plain_backup: BuiltBackup) -> None:
    device = AppleBackup(plain_backup.path).device_info()
    assert device.device_name == "Test iPhone"
    assert device.product_type == "iPhone15,2"
    assert device.product_version == "17.5.1"
    assert THREEMA_BUNDLE_ID in device.installed_applications


def test_applications_are_confirmed_against_info_plist(plain_backup: BuiltBackup) -> None:
    applications = AppleBackup(plain_backup.path).applications()
    threema = next(app for app in applications if app.bundle_id == THREEMA_BUNDLE_ID)
    assert threema.confirmed_installed
    assert threema.bundle_version == "6.1.2"


def test_application_only_in_manifest_is_not_confirmed(tmp_path: Path) -> None:
    """Eine App, die nur in Manifest.plist steht, gilt nicht als bestaetigt."""
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-x", "Documents/a.bin", b"x")],
        installed_applications=["ch.threema.iapp"],
    )
    # Info.plist so aendern, dass die App dort fehlt.
    info_path = backup.path / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["Installed Applications"] = []
    info_path.write_bytes(plistlib.dumps(info))

    applications = AppleBackup(backup.path).applications()
    threema = next(app for app in applications if app.bundle_id == "ch.threema.iapp")
    assert not threema.confirmed_installed


def test_application_only_in_info_plist_is_still_reported(tmp_path: Path) -> None:
    """Eine App darf nicht verloren gehen, nur weil Manifest.plist sie nicht nennt."""
    backup = build_backup(
        tmp_path / "b", [BackupFile("AppDomain-x", "Documents/a.bin", b"x")]
    )
    info_path = backup.path / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["Installed Applications"] = ["ch.threema.iapp"]
    info_path.write_bytes(plistlib.dumps(info))

    applications = AppleBackup(backup.path).applications()
    assert any(app.bundle_id == "ch.threema.iapp" and app.confirmed_installed
               for app in applications)


def test_info_reports_encryption_state(encrypted_backup: BuiltBackup) -> None:
    info = AppleBackup(encrypted_backup.path).info()
    assert info.is_encrypted
    assert info.has_manifest_key


def test_encryption_state_exposes_keybag(encrypted_backup: BuiltBackup) -> None:
    encryption = AppleBackup(encrypted_backup.path).encryption
    assert encryption.is_encrypted
    assert encryption.keybag is not None and len(encryption.keybag) > 100
    assert encryption.manifest_key is not None and len(encryption.manifest_key) == 44
    assert encryption.manifest_is_encrypted


def test_plain_backup_has_no_keybag(plain_backup: BuiltBackup) -> None:
    encryption = AppleBackup(plain_backup.path).encryption
    assert not encryption.is_encrypted
    assert encryption.keybag is None
    assert encryption.manifest_key is None
    assert not encryption.manifest_is_encrypted


def test_unreadable_plist_does_not_raise(tmp_path: Path) -> None:
    """Ein kaputtes Info.plist darf das Backup nicht unbrauchbar machen."""
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-x", "Documents/a.bin", b"x")],
        installed_applications=["x"],
    )
    (backup.path / "Info.plist").write_bytes(b"kein plist")
    device = AppleBackup(backup.path).device_info()
    assert device.device_name is None


# ---------------------------------------------------------------------------
# Nutzdaten
# ---------------------------------------------------------------------------


def test_payload_path_uses_two_character_subdirectory(plain_backup: BuiltBackup) -> None:
    backup = AppleBackup(plain_backup.path)
    entry = plain_backup.file_by_path("Documents/img/photo1.jpg")
    path = backup.payload_path(entry.file_id)
    assert path.parent.name == entry.file_id[:2]
    assert path.name == entry.file_id


def test_payload_read_returns_content(plain_backup: BuiltBackup) -> None:
    backup = AppleBackup(plain_backup.path)
    entry = plain_backup.file_by_path("Documents/img/photo1.jpg")
    assert backup.read_payload(entry.file_id) == entry.content


def test_missing_payload_is_reported(plain_backup: BuiltBackup) -> None:
    backup = AppleBackup(plain_backup.path)
    entry = plain_backup.file_by_path("Documents/fehlt.jpg")
    assert not backup.payload_exists(entry.file_id)


def test_payload_directories_are_hex_only(plain_backup: BuiltBackup) -> None:
    backup = AppleBackup(plain_backup.path)
    (plain_backup.path / "zz").mkdir()
    names = [p.name for p in backup.payload_directories()]
    assert "zz" not in names
    assert all(len(name) == 2 and int(name, 16) >= 0 for name in names)


def test_present_metadata_files(plain_backup: BuiltBackup) -> None:
    assert AppleBackup(plain_backup.path).present_metadata_files() == (
        "Info.plist",
        "Manifest.plist",
        "Manifest.db",
        "Status.plist",
    )


# ---------------------------------------------------------------------------
# Auflisten
# ---------------------------------------------------------------------------


def test_list_local_backups_finds_backups(tmp_path: Path) -> None:
    root = tmp_path / "Backup"
    build_backup(root, sample_files(), udid="AAA", installed_applications=[])
    build_backup(root, sample_files(), udid="BBB", installed_applications=[])
    (root / "nicht-ein-backup").mkdir()
    found = list_local_backups(root)
    assert {p.name for p in found} == {"AAA", "BBB"}


def test_list_local_backups_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert list_local_backups(tmp_path / "gibt-es-nicht") == ()


def test_permission_error_mentions_full_disk_access(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Backup"
    root.mkdir()

    def deny(_self: Path) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "iterdir", deny)
    with pytest.raises(BackupAccessError, match="Festplattenvollzugriff"):
        list_local_backups(root)


def test_repr_does_not_leak_path(plain_backup: BuiltBackup) -> None:
    text = repr(AppleBackup(plain_backup.path))
    assert str(plain_backup.path.parent) not in text
