"""Tests fuer den Fixture-Generator selbst.

Wenn der Generator falsche Backups baut, beweisen alle anderen Tests nichts.
Diese Tests pruefen daher, dass die erzeugten Backups dem echten Format
entsprechen und mit den dokumentierten Verfahren entschluesselbar sind.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

from tests.conftest import TEST_PASSWORD
from tests.support.backup_builder import (
    BackupFile,
    BuiltBackup,
    build_backup,
    derive_passcode_key,
)

# ---------------------------------------------------------------------------
# Keybag-Parsing (hier bewusst dupliziert, um den Fixture-Generator unabhaengig
# vom Produktionscode zu pruefen)
# ---------------------------------------------------------------------------


def parse_keybag_tlv(blob: bytes) -> tuple[dict[str, bytes], list[dict[str, bytes]]]:
    """Minimaler TLV-Parser fuer den Test."""
    header: dict[str, bytes] = {}
    classes: list[dict[str, bytes]] = []
    current: dict[str, bytes] | None = None
    offset = 0
    while offset + 8 <= len(blob):
        tag = blob[offset : offset + 4].decode("ascii")
        length = struct.unpack(">I", blob[offset + 4 : offset + 8])[0]
        value = blob[offset + 8 : offset + 8 + length]
        offset += 8 + length
        if tag == "UUID" and "SALT" in header:
            current = {}
            classes.append(current)
        target = current if current is not None else header
        target[tag] = value
    return header, classes


def _int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


# ---------------------------------------------------------------------------
# Struktur
# ---------------------------------------------------------------------------


def test_plain_backup_has_expected_layout(plain_backup: BuiltBackup) -> None:
    for name in ("Info.plist", "Manifest.plist", "Manifest.db", "Status.plist"):
        assert (plain_backup.path / name).is_file(), name
    assert not plain_backup.is_encrypted


def test_file_id_is_sha1_of_domain_dash_relative_path(plain_backup: BuiltBackup) -> None:
    entry = plain_backup.file_by_path("Documents/img/photo1.jpg")
    expected = hashlib.sha1(f"{entry.domain}-{entry.relative_path}".encode()).hexdigest()
    assert entry.file_id == expected
    assert (plain_backup.path / entry.file_id[:2] / entry.file_id).is_file()


def test_plain_payload_is_stored_verbatim(plain_backup: BuiltBackup) -> None:
    entry = plain_backup.file_by_path("Documents/img/photo1.jpg")
    stored = (plain_backup.path / entry.file_id[:2] / entry.file_id).read_bytes()
    assert stored == entry.content
    assert plain_backup.expected_hashes[entry.file_id] == hashlib.sha256(entry.content).hexdigest()


def test_manifest_db_is_readable_sqlite_when_plain(plain_backup: BuiltBackup) -> None:
    uri = f"file:{plain_backup.path / 'Manifest.db'}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(Files)")]
        assert columns == ["fileID", "domain", "relativePath", "flags", "file"]
        count = connection.execute("SELECT COUNT(*) FROM Files").fetchone()[0]
    assert count == len(plain_backup.files)


def test_omitted_and_truncated_payloads_behave_as_declared(plain_backup: BuiltBackup) -> None:
    missing = plain_backup.file_by_path("Documents/fehlt.jpg")
    assert not (plain_backup.path / missing.file_id[:2] / missing.file_id).exists()

    truncated = plain_backup.file_by_path("Documents/abgeschnitten.mp4")
    stored = (plain_backup.path / truncated.file_id[:2] / truncated.file_id).read_bytes()
    assert len(stored) == 32
    assert len(stored) < truncated.size


def test_directory_entries_have_no_payload(plain_backup: BuiltBackup) -> None:
    directory = plain_backup.file_by_path("Documents/img")
    assert directory.flags == 2
    assert not (plain_backup.path / directory.file_id[:2] / directory.file_id).exists()


# ---------------------------------------------------------------------------
# MBFile-Blob
# ---------------------------------------------------------------------------


def _mbfile(backup: BuiltBackup, relative_path: str) -> dict[str, object]:
    entry = backup.file_by_path(relative_path)
    uri = f"file:{backup.path / 'Manifest.db'}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        blob = connection.execute(
            "SELECT file FROM Files WHERE fileID = ?", (entry.file_id,)
        ).fetchone()[0]
    plist = plistlib.loads(blob)
    assert plist["$archiver"] == "NSKeyedArchiver"
    return plist["$objects"][plist["$top"]["root"].data], plist  # type: ignore[return-value]


def test_mbfile_blob_carries_size_and_protection_class(plain_backup: BuiltBackup) -> None:
    root, plist = _mbfile(plain_backup, "Documents/img/photo1.jpg")
    entry = plain_backup.file_by_path("Documents/img/photo1.jpg")
    assert root["Size"] == entry.size
    assert root["ProtectionClass"] == entry.protection_class
    assert plist["$objects"][root["RelativePath"].data] == entry.relative_path
    assert plist["$objects"][root["$class"].data]["$classname"] == "MBFile"


def test_plain_backup_has_no_encryption_key(plain_backup: BuiltBackup) -> None:
    root, _ = _mbfile(plain_backup, "Documents/img/photo1.jpg")
    assert "EncryptionKey" not in root


def test_corrupt_metadata_blob_is_not_a_valid_plist(plain_backup: BuiltBackup) -> None:
    entry = plain_backup.file_by_path("Documents/kaputte-metadaten.jpg")
    uri = f"file:{plain_backup.path / 'Manifest.db'}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        blob = connection.execute(
            "SELECT file FROM Files WHERE fileID = ?", (entry.file_id,)
        ).fetchone()[0]
    with pytest.raises(plistlib.InvalidFileException):
        plistlib.loads(blob)


# ---------------------------------------------------------------------------
# Verschluesselung: vollstaendiger Round-Trip
# ---------------------------------------------------------------------------


def test_encrypted_backup_hides_manifest_and_payloads(encrypted_backup: BuiltBackup) -> None:
    manifest = (encrypted_backup.path / "Manifest.db").read_bytes()
    assert not manifest.startswith(b"SQLite format 3")

    entry = encrypted_backup.file_by_path("Documents/img/photo1.jpg")
    stored = (encrypted_backup.path / entry.file_id[:2] / entry.file_id).read_bytes()
    assert stored != entry.content
    assert len(stored) % 16 == 0


def test_full_decryption_round_trip(encrypted_backup: BuiltBackup) -> None:
    """Beweist, dass das Fixture mit dem dokumentierten Verfahren lesbar ist.

    Dieser Test ist die Vorbedingung fuer Phase 2: er zeigt, dass Keybag,
    Klassenschluessel, ManifestKey und Dateischluessel konsistent erzeugt
    wurden.
    """
    manifest_plist = plistlib.loads((encrypted_backup.path / "Manifest.plist").read_bytes())
    header, class_blocks = parse_keybag_tlv(manifest_plist["BackupKeyBag"])

    # 1. Passcode-Key ableiten
    passcode_key = derive_passcode_key(
        TEST_PASSWORD,
        salt=header["SALT"],
        iterations=_int(header["ITER"]),
        dpsl=header["DPSL"],
        dpic=_int(header["DPIC"]),
    )

    # 2. Klassenschluessel entpacken
    class_keys = {
        _int(block["CLAS"]): aes_key_unwrap(passcode_key, block["WPKY"]) for block in class_blocks
    }
    assert len(class_keys) == 11

    # 3. Manifest.db entschluesseln
    manifest_key_blob = manifest_plist["ManifestKey"]
    manifest_class = struct.unpack("<I", manifest_key_blob[:4])[0]
    manifest_key = aes_key_unwrap(class_keys[manifest_class], manifest_key_blob[4:])
    decrypted = aes_cbc_decrypt(manifest_key, (encrypted_backup.path / "Manifest.db").read_bytes())
    assert decrypted.startswith(b"SQLite format 3\x00")

    # 4. Eine Nutzdatei entschluesseln und gegen den Klartext pruefen
    plain_manifest = encrypted_backup.path.parent / "decrypted-manifest.db"
    plain_manifest.write_bytes(decrypted)
    entry = encrypted_backup.file_by_path("Documents/img/photo1.jpg")
    with sqlite3.connect(f"file:{plain_manifest}?mode=ro", uri=True) as connection:
        blob = connection.execute(
            "SELECT file FROM Files WHERE fileID = ?", (entry.file_id,)
        ).fetchone()[0]
    plist = plistlib.loads(blob)
    root = plist["$objects"][plist["$top"]["root"].data]
    key_blob = plist["$objects"][root["EncryptionKey"].data]["NS.data"]
    assert len(key_blob) == 44
    file_key = aes_key_unwrap(class_keys[root["ProtectionClass"]], key_blob[4:])

    stored = (encrypted_backup.path / entry.file_id[:2] / entry.file_id).read_bytes()
    recovered = aes_cbc_decrypt(file_key, stored)[: root["Size"]]
    assert recovered == entry.content
    assert hashlib.sha256(recovered).hexdigest() == encrypted_backup.expected_hashes[entry.file_id]


def test_wrong_password_fails_key_unwrap(encrypted_backup: BuiltBackup) -> None:
    """Ein falsches Passwort muss an der Integritaetspruefung scheitern."""
    manifest_plist = plistlib.loads((encrypted_backup.path / "Manifest.plist").read_bytes())
    header, class_blocks = parse_keybag_tlv(manifest_plist["BackupKeyBag"])
    wrong_key = derive_passcode_key(
        "falsches-passwort",
        salt=header["SALT"],
        iterations=_int(header["ITER"]),
        dpsl=header["DPSL"],
        dpic=_int(header["DPIC"]),
    )
    with pytest.raises(InvalidUnwrap):
        aes_key_unwrap(wrong_key, class_blocks[0]["WPKY"])


def test_builder_is_deterministic(tmp_path: Path) -> None:
    """Identische Eingaben ergeben identische Backups - erleichtert Fehlersuche."""
    files = [BackupFile("AppDomain-x", "Documents/a.bin", b"payload")]
    first = build_backup(tmp_path / "a", list(files), password="pw")
    second = build_backup(tmp_path / "b", list(files), password="pw")
    entry = files[0]
    assert (first.path / entry.file_id[:2] / entry.file_id).read_bytes() == (
        second.path / entry.file_id[:2] / entry.file_id
    ).read_bytes()


def test_unknown_protection_class_is_rejected(tmp_path: Path) -> None:
    """Der Builder darf keine Datei mit Klasse ohne Schluessel erzeugen."""
    with pytest.raises(ValueError, match="Protection Class"):
        build_backup(
            tmp_path / "bad",
            [BackupFile("AppDomain-x", "Documents/a.bin", b"x", protection_class=99)],
            password="pw",
        )
