"""Tests fuer die Backup-Session.

Die Session entscheidet, ob das Manifest lesbar ist, fragt das Passwort nur wenn
noetig, und legt entschluesselte Zwischendateien ausschliesslich ausserhalb des
Backups ab.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.encryption import WrongPasswordError
from msgbackup_extractor.core.manifest import ManifestReader
from msgbackup_extractor.core.session import (
    MANIFEST_WORK_NAME,
    BackupSession,
    interactive_password,
)
from msgbackup_extractor.core.sqlite_ro import NotASQLiteDatabase
from tests.conftest import TEST_PASSWORD, sample_files
from tests.support.backup_builder import BuiltBackup, build_backup


def _session(backup: BuiltBackup, *, password: str | None = None, **kwargs: object):
    provider = (lambda: password) if password is not None else None
    return BackupSession(
        AppleBackup(backup.path), password_provider=provider, **kwargs  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Unverschluesselte Backups
# ---------------------------------------------------------------------------


def test_plain_backup_needs_no_password(plain_backup: BuiltBackup) -> None:
    calls: list[int] = []

    def should_not_be_called() -> str:
        calls.append(1)
        return "x"

    with BackupSession(
        AppleBackup(plain_backup.path), password_provider=should_not_be_called
    ) as session:
        assert session.manifest.is_available
        assert not session.manifest.was_decrypted
        assert session.manifest.path == plain_backup.path / "Manifest.db"
    assert calls == [], "Bei einem unverschluesselten Backup darf nicht gefragt werden"


def test_plain_backup_creates_no_work_directory(plain_backup: BuiltBackup) -> None:
    with _session(plain_backup) as session:
        assert session.work_dir is None
        assert session.keys is None


# ---------------------------------------------------------------------------
# Verschluesselt, ohne Passwort
# ---------------------------------------------------------------------------


def test_encrypted_without_provider_gives_partial_access(
    encrypted_backup: BuiltBackup,
) -> None:
    with _session(encrypted_backup) as session:
        assert not session.manifest.is_available
        assert session.keys is None
        assert "Passwort" in (session.manifest.unavailable_reason or "")


# ---------------------------------------------------------------------------
# Verschluesselt, mit Passwort
# ---------------------------------------------------------------------------


def test_correct_password_makes_the_manifest_readable(
    encrypted_backup: BuiltBackup,
) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        assert session.manifest.is_available
        assert session.manifest.was_decrypted
        assert session.manifest.path.name == MANIFEST_WORK_NAME
        with ManifestReader(session.manifest.path) as reader:
            assert reader.count() == len(encrypted_backup.files)


def test_keys_are_available_for_payloads(encrypted_backup: BuiltBackup) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        assert session.has_keys
        assert session.keys is not None
        assert len(session.keys.available_classes) == 11


def test_wrong_password_raises(encrypted_backup: BuiltBackup) -> None:
    with pytest.raises(WrongPasswordError), _session(encrypted_backup, password="falsch"):
        pass


def test_password_is_only_requested_once(encrypted_backup: BuiltBackup) -> None:
    calls: list[int] = []

    def provider() -> str:
        calls.append(1)
        return TEST_PASSWORD

    with BackupSession(
        AppleBackup(encrypted_backup.path), password_provider=provider
    ) as session:
        assert session.manifest.is_available
    assert calls == [1]


# ---------------------------------------------------------------------------
# Arbeitsverzeichnis
# ---------------------------------------------------------------------------


def test_decrypted_manifest_is_never_inside_the_backup(
    encrypted_backup: BuiltBackup,
) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        assert session.work_dir is not None
        assert encrypted_backup.path not in session.work_dir.parents
        assert session.work_dir != encrypted_backup.path
        assert encrypted_backup.path not in session.manifest.path.parents


def test_temporary_work_directory_is_removed_on_close(
    encrypted_backup: BuiltBackup,
) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        work_dir = session.work_dir
        assert work_dir is not None and work_dir.is_dir()
    assert not work_dir.exists()


def test_explicit_work_directory_is_kept(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    """Ein ausdruecklich uebergebenes Verzeichnis gehoert dem Aufrufer."""
    work_dir = tmp_path / "arbeit"
    with _session(encrypted_backup, password=TEST_PASSWORD, work_dir=work_dir) as session:
        assert session.work_dir == work_dir
    assert work_dir.is_dir()
    assert (work_dir / MANIFEST_WORK_NAME).is_file()


def test_keys_are_wiped_on_close(encrypted_backup: BuiltBackup) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        keys = session.keys
        assert keys is not None
        secrets = list(keys.class_keys.values())
    assert session.keys is None
    assert all(secret.is_wiped for secret in secrets)


def test_ensure_work_dir_is_idempotent(encrypted_backup: BuiltBackup) -> None:
    with _session(encrypted_backup, password=TEST_PASSWORD) as session:
        assert session.ensure_work_dir() == session.ensure_work_dir()


# ---------------------------------------------------------------------------
# Sonderfaelle
# ---------------------------------------------------------------------------


def test_backup_without_manifest_key_is_read_directly(tmp_path: Path) -> None:
    """Aeltere Backups: Nutzdaten verschluesselt, Manifest.db im Klartext."""
    backup = build_backup(
        tmp_path / "alt",
        sample_files(),
        password=TEST_PASSWORD,
        encrypt_manifest=False,
        installed_applications=[],
    )
    with _session(backup, password=TEST_PASSWORD) as session:
        assert session.manifest.is_available
        assert not session.manifest.was_decrypted
        assert session.has_keys


def test_broken_keybag_yields_a_reason_not_a_crash(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b", sample_files(), password="pw", installed_applications=[]
    )
    manifest_path = backup.path / "Manifest.plist"
    manifest = plistlib.loads(manifest_path.read_bytes())
    manifest["BackupKeyBag"] = b"das ist kein keybag"
    manifest_path.write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))

    with _session(backup, password="pw") as session:
        assert not session.manifest.is_available
        assert session.manifest.unavailable_reason is not None
        assert session.keys is None


def test_missing_manifest_key_blob_is_handled(tmp_path: Path) -> None:
    """Verschluesselt, aber ohne ManifestKey - das Manifest gilt als Klartext."""
    backup = build_backup(
        tmp_path / "b",
        sample_files(),
        password="pw",
        omit_manifest_key=True,
        installed_applications=[],
    )
    with _session(backup, password="pw") as session:
        # Das Manifest ist tatsaechlich verschluesselt, also nicht lesbar - aber
        # die Session stuerzt nicht ab, sondern liefert einen Grund.
        assert session.has_keys
        with (
            pytest.raises(NotASQLiteDatabase),
            ManifestReader(session.manifest.path),
        ):
            pass


def test_repr_reflects_the_state(
    plain_backup: BuiltBackup, encrypted_backup: BuiltBackup
) -> None:
    with _session(plain_backup) as session:
        assert "encrypted=False" in repr(session)
        assert "manifest_available=True" in repr(session)
    with _session(encrypted_backup) as session:
        assert "encrypted=True" in repr(session)
        assert "manifest_available=False" in repr(session)


def test_close_is_idempotent(encrypted_backup: BuiltBackup) -> None:
    session = _session(encrypted_backup, password=TEST_PASSWORD)
    session.__enter__()
    session.close()
    session.close()
    assert session.keys is None


def test_interactive_password_uses_getpass(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        return "geheim"

    monkeypatch.setattr("getpass.getpass", fake)
    assert interactive_password() == "geheim"
    assert prompts and "Passwort" in prompts[0]
