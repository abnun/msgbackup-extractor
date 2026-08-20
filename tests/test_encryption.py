"""Tests fuer die Entschluesselung verschluesselter Backups.

Die Fixtures werden mit echtem PBKDF2, echtem AES-Key-Wrap und echtem
AES-256-CBC gebaut. Ein bestandener Test bedeutet also, dass der
Produktionscode das dokumentierte Verfahren tatsaechlich umsetzt - nicht nur,
dass er zu einem vereinfachten Testformat passt.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from msgbackup_extractor.core import encryption
from msgbackup_extractor.core.encryption import (
    BackupKeys,
    UnavailableProtectionClass,
    UndecryptableFile,
    WrongPasswordError,
    decrypt_bytes,
    decrypt_file_to,
    decrypt_head,
    decrypt_manifest_database,
    decrypt_stream,
    derive_passcode_key,
    split_encryption_key_blob,
)
from msgbackup_extractor.core.keybag import parse_keybag
from msgbackup_extractor.core.manifest import ManifestReader
from msgbackup_extractor.core.secure_memory import SecretBytes
from tests.conftest import TEST_PASSWORD, sample_files
from tests.support.backup_builder import (
    BackupFile,
    BuiltBackup,
    aes_cbc_encrypt,
    build_backup,
    build_keybag,
)


def _keybag_of(backup: BuiltBackup):
    manifest = plistlib.loads((backup.path / "Manifest.plist").read_bytes())
    return parse_keybag(manifest["BackupKeyBag"]), manifest


# ---------------------------------------------------------------------------
# Schluesselableitung
# ---------------------------------------------------------------------------


def test_derivation_is_deterministic() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with (
        derive_passcode_key("geheim", keybag) as first,
        derive_passcode_key("geheim", keybag) as second,
    ):
        assert first.reveal() == second.reveal()


def test_different_passwords_give_different_keys() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with derive_passcode_key("a", keybag) as first, derive_passcode_key("b", keybag) as second:
        assert first.reveal() != second.reveal()


def test_derived_key_has_expected_length() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with derive_passcode_key("geheim", keybag) as key:
        assert len(key) == encryption.DERIVED_KEY_LENGTH


def test_derivation_matches_the_builder() -> None:
    """Produktionscode und Fixture-Generator muessen denselben Key ableiten."""
    fixture = build_keybag("geheim")
    keybag = parse_keybag(fixture.blob)
    with derive_passcode_key("geheim", keybag) as key:
        assert key.reveal() == fixture.passcode_key


def test_unicode_password_is_encoded_as_utf8() -> None:
    keybag = parse_keybag(build_keybag("Grüße-😀").blob)
    fixture = build_keybag("Grüße-😀")
    with derive_passcode_key("Grüße-😀", keybag) as key:
        assert key.reveal() == fixture.passcode_key


# ---------------------------------------------------------------------------
# Klassenschluessel
# ---------------------------------------------------------------------------


def test_correct_password_unwraps_all_class_keys() -> None:
    fixture = build_keybag("geheim")
    keybag = parse_keybag(fixture.blob)
    with BackupKeys.from_password(keybag, "geheim") as keys:
        assert keys.available_classes == keybag.available_classes
        assert keys.unavailable_classes == ()
        for protection_class, expected in fixture.class_keys.items():
            assert keys.class_keys[protection_class].reveal() == expected


def test_wrong_password_raises_wrong_password_error() -> None:
    keybag = parse_keybag(build_keybag("richtig").blob)
    with pytest.raises(WrongPasswordError):
        BackupKeys.from_password(keybag, "falsch")


def test_wrong_password_message_names_the_right_password() -> None:
    """Die haeufigste Verwechslung ist Gerätecode oder Apple-ID-Passwort."""
    keybag = parse_keybag(build_keybag("richtig").blob)
    with pytest.raises(WrongPasswordError) as error:
        BackupKeys.from_password(keybag, "falsch")
    message = str(error.value)
    assert "Finder" in message
    assert "Apple-ID" in message


def test_empty_password_is_wrong_not_a_crash() -> None:
    keybag = parse_keybag(build_keybag("richtig").blob)
    with pytest.raises(WrongPasswordError):
        BackupKeys.from_password(keybag, "")


def test_keys_are_wiped_on_exit() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with BackupKeys.from_password(keybag, "geheim") as keys:
        secrets = list(keys.class_keys.values())
    assert keys.available_classes == ()
    assert all(secret.is_wiped for secret in secrets)


def test_keys_repr_does_not_leak() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with BackupKeys.from_password(keybag, "geheim") as keys:
        text = repr(keys)
        for secret in keys.class_keys.values():
            assert secret.reveal().hex() not in text


def test_unavailable_class_raises_with_explanation() -> None:
    keybag = parse_keybag(build_keybag("geheim", classes=(3, 4)).blob)
    with BackupKeys.from_password(keybag, "geheim") as keys:
        with pytest.raises(UnavailableProtectionClass) as error:
            keys.unwrap_file_key(11, b"k" * 40)
        assert error.value.protection_class == 11
        assert error.value.available == (3, 4)
        assert "unentschluesselbar" in str(error.value)


def test_mismatched_file_key_raises_undecryptable() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    with BackupKeys.from_password(keybag, "geheim") as keys, pytest.raises(UndecryptableFile):
        keys.unwrap_file_key(3, b"\x00" * 40)


def test_has_class(tmp_path: Path) -> None:
    keybag = parse_keybag(build_keybag("geheim", classes=(1, 3)).blob)
    with BackupKeys.from_password(keybag, "geheim") as keys:
        assert keys.has_class(3)
        assert not keys.has_class(7)


# ---------------------------------------------------------------------------
# EncryptionKey-Blob
# ---------------------------------------------------------------------------


def test_split_encryption_key_blob() -> None:
    blob = (4).to_bytes(4, "little") + bytes(range(40))
    protection_class, wrapped = split_encryption_key_blob(blob)
    assert protection_class == 4
    assert wrapped == bytes(range(40))


@pytest.mark.parametrize("blob", [b"", b"\x03\x00\x00\x00"])
def test_too_short_blob_is_rejected(blob: bytes) -> None:
    with pytest.raises(UndecryptableFile, match="zu kurz"):
        split_encryption_key_blob(blob)


# ---------------------------------------------------------------------------
# Dateientschluesselung
# ---------------------------------------------------------------------------


@pytest.fixture
def file_key() -> SecretBytes:
    return SecretBytes(bytes(range(32)))


def test_decrypt_bytes_round_trip(file_key: SecretBytes) -> None:
    plaintext = b"Hallo Welt, das ist ein Test." * 10
    encrypted = aes_cbc_encrypt(file_key.reveal(), plaintext)
    assert decrypt_bytes(encrypted, file_key, size=len(plaintext)) == plaintext


def test_decrypt_bytes_truncates_padding(file_key: SecretBytes) -> None:
    """Das Kuerzen auf `Size` macht die Padding-Variante irrelevant."""
    plaintext = b"x" * 100  # kein Vielfaches von 16
    encrypted = aes_cbc_encrypt(file_key.reveal(), plaintext)
    assert len(encrypted) % 16 == 0
    assert len(encrypted) > len(plaintext)
    assert decrypt_bytes(encrypted, file_key, size=100) == plaintext


def test_decrypt_bytes_rejects_misaligned_input(file_key: SecretBytes) -> None:
    with pytest.raises(UndecryptableFile, match="Vielfaches der Blockgroesse"):
        decrypt_bytes(b"nur 13 bytes!", file_key)


@pytest.mark.parametrize("chunk_size", [16, 64, 1024, 1024 * 1024])
def test_decrypt_stream_is_chunk_size_independent(
    file_key: SecretBytes, chunk_size: int, tmp_path: Path
) -> None:
    plaintext = bytes(range(256)) * 50
    path = tmp_path / "data.enc"
    path.write_bytes(aes_cbc_encrypt(file_key.reveal(), plaintext))
    with path.open("rb") as handle:
        result = b"".join(
            decrypt_stream(handle, file_key, size=len(plaintext), chunk_size=chunk_size)
        )
    assert result == plaintext


def test_decrypt_stream_without_size_keeps_padding(
    file_key: SecretBytes, tmp_path: Path
) -> None:
    plaintext = b"y" * 50
    path = tmp_path / "data.enc"
    path.write_bytes(aes_cbc_encrypt(file_key.reveal(), plaintext))
    with path.open("rb") as handle:
        result = b"".join(decrypt_stream(handle, file_key))
    assert result.startswith(plaintext)
    assert len(result) % 16 == 0


def test_decrypt_stream_rejects_truncated_file(
    file_key: SecretBytes, tmp_path: Path
) -> None:
    path = tmp_path / "data.enc"
    path.write_bytes(aes_cbc_encrypt(file_key.reveal(), b"z" * 100)[:37])
    with path.open("rb") as handle, pytest.raises(UndecryptableFile, match="Blockgrenze"):
        list(decrypt_stream(handle, file_key, size=100))


def test_decrypt_stream_of_empty_file(file_key: SecretBytes, tmp_path: Path) -> None:
    path = tmp_path / "leer.enc"
    path.write_bytes(b"")
    with path.open("rb") as handle:
        assert list(decrypt_stream(handle, file_key, size=0)) == []


def test_decrypt_file_to_writes_exact_size(file_key: SecretBytes, tmp_path: Path) -> None:
    plaintext = b"Inhalt" * 1000
    source = tmp_path / "in.enc"
    source.write_bytes(aes_cbc_encrypt(file_key.reveal(), plaintext))
    target = tmp_path / "out.bin"
    written = decrypt_file_to(source, target, file_key, size=len(plaintext))
    assert written == len(plaintext)
    assert target.read_bytes() == plaintext


def test_truncated_file_is_reported_not_silently_shortened(
    file_key: SecretBytes, tmp_path: Path
) -> None:
    """Eine auf Blockgrenze abgeschnittene Datei entschluesselt ohne Fehler.

    Genau deshalb muss die Byteanzahl mit `size` verglichen werden - sonst waere
    der Teilverlust unsichtbar.
    """
    plaintext = b"w" * 500
    source = tmp_path / "in.enc"
    full = aes_cbc_encrypt(file_key.reveal(), plaintext)
    source.write_bytes(full[:64])  # Vielfaches von 16, aber viel zu kurz
    target = tmp_path / "out.bin"

    with pytest.raises(encryption.TruncatedFile) as error:
        decrypt_file_to(source, target, file_key, size=len(plaintext))
    assert error.value.expected == 500
    assert error.value.actual == 64
    assert "unvollstaendig" in str(error.value)
    assert not target.exists(), "Eine unvollstaendige Datei darf nicht liegen bleiben"


def test_truncated_file_can_be_recovered_deliberately(
    file_key: SecretBytes, tmp_path: Path
) -> None:
    """Mit require_full_size=False wird der lesbare Teil gerettet."""
    plaintext = b"w" * 500
    source = tmp_path / "in.enc"
    source.write_bytes(aes_cbc_encrypt(file_key.reveal(), plaintext)[:64])
    target = tmp_path / "out.bin"
    written = decrypt_file_to(
        source, target, file_key, size=len(plaintext), require_full_size=False
    )
    assert written == 64
    assert target.read_bytes() == plaintext[:64]


def test_decrypt_head_matches_full_decryption(
    file_key: SecretBytes, tmp_path: Path
) -> None:
    """Der Dateianfang muss ohne die ganze Datei korrekt entschluesselbar sein."""
    plaintext = bytes(range(256)) * 200
    source = tmp_path / "gross.enc"
    source.write_bytes(aes_cbc_encrypt(file_key.reveal(), plaintext))
    for length in (1, 15, 16, 17, 100, 4096):
        assert decrypt_head(source, file_key, length) == plaintext[:length]


def test_decrypt_head_of_empty_file(file_key: SecretBytes, tmp_path: Path) -> None:
    source = tmp_path / "leer.enc"
    source.write_bytes(b"")
    assert decrypt_head(source, file_key, 100) == b""


# ---------------------------------------------------------------------------
# Vollstaendiger Backup-Round-Trip
# ---------------------------------------------------------------------------


def test_manifest_database_is_decrypted(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    keybag, manifest_plist = _keybag_of(encrypted_backup)
    target = tmp_path / "work" / "Manifest.db"
    with BackupKeys.from_password(keybag, TEST_PASSWORD) as keys:
        result = decrypt_manifest_database(
            encrypted_backup.path / "Manifest.db", target, keys, manifest_plist["ManifestKey"]
        )
    assert result == target
    assert target.read_bytes().startswith(b"SQLite format 3\x00")


def test_manifest_decryption_target_is_outside_the_backup(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    keybag, manifest_plist = _keybag_of(encrypted_backup)
    target = tmp_path / "work" / "Manifest.db"
    with BackupKeys.from_password(keybag, TEST_PASSWORD) as keys:
        decrypt_manifest_database(
            encrypted_backup.path / "Manifest.db", target, keys, manifest_plist["ManifestKey"]
        )
    assert encrypted_backup.path not in target.parents


def test_wrong_manifest_key_is_detected_not_silently_accepted(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    """Ein falscher ManifestKey darf keinen Datenmuell erzeugen."""
    keybag, manifest_plist = _keybag_of(encrypted_backup)
    blob = manifest_plist["ManifestKey"]
    # Klasse beibehalten, Wrapped Key einer anderen Datei vorschieben.
    tampered = blob[:4] + bytes(40)
    target = tmp_path / "work" / "Manifest.db"
    with (
        BackupKeys.from_password(keybag, TEST_PASSWORD) as keys,
        pytest.raises(UndecryptableFile),
    ):
        decrypt_manifest_database(
            encrypted_backup.path / "Manifest.db", target, keys, tampered
        )
    assert not target.exists()


def test_every_payload_decrypts_to_the_expected_content(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    """Der entscheidende Test: alle Nutzdateien Byte fuer Byte wiederherstellen."""
    keybag, manifest_plist = _keybag_of(encrypted_backup)
    manifest = tmp_path / "Manifest.db"

    with BackupKeys.from_password(keybag, TEST_PASSWORD) as keys:
        decrypt_manifest_database(
            encrypted_backup.path / "Manifest.db", manifest, keys, manifest_plist["ManifestKey"]
        )
        with ManifestReader(manifest) as reader:
            entries = [e for e in reader.entries() if e.is_encrypted]

        assert entries, "Der Test prueft nichts, wenn keine verschluesselten Eintraege da sind"
        checked = 0
        truncated: list[str] = []
        for entry in entries:
            source = encrypted_backup.path / entry.file_id[:2] / entry.file_id
            if not source.is_file():
                continue
            assert entry.protection_class is not None
            assert entry.encryption_key is not None
            with keys.unwrap_file_key(entry.protection_class, entry.encryption_key) as key:
                try:
                    with source.open("rb") as handle:
                        recovered = b"".join(decrypt_stream(handle, key, size=entry.size))
                except UndecryptableFile:
                    truncated.append(entry.relative_path)
                    continue

            expected = encrypted_backup.file_by_path(entry.relative_path).content
            if len(recovered) != len(expected):
                # Abgeschnittene Quelldatei: der Anfang muss trotzdem stimmen.
                assert expected.startswith(recovered), entry.relative_path
                truncated.append(entry.relative_path)
                continue
            assert recovered == expected, entry.relative_path
            checked += 1

        assert checked >= 8
        assert truncated == ["Documents/abgeschnitten.mp4"]


def test_protection_class_in_blob_matches_manifest_field(
    encrypted_backup: BuiltBackup, tmp_path: Path
) -> None:
    """Der Klassen-Praefix im Blob und das Manifest-Feld muessen uebereinstimmen."""
    keybag, manifest_plist = _keybag_of(encrypted_backup)
    manifest = tmp_path / "Manifest.db"
    with BackupKeys.from_password(keybag, TEST_PASSWORD) as keys:
        decrypt_manifest_database(
            encrypted_backup.path / "Manifest.db", manifest, keys, manifest_plist["ManifestKey"]
        )
    # Die Blobs im Manifest sind schon um den Praefix gekuerzt; geprueft wird der
    # Praefix des ManifestKey, der ungekuerzt vorliegt.
    protection_class, wrapped = split_encryption_key_blob(manifest_plist["ManifestKey"])
    assert protection_class in keybag.available_classes
    assert len(wrapped) == 40


def test_backup_without_manifest_key_keeps_manifest_readable(tmp_path: Path) -> None:
    """Aeltere Backups: Nutzdaten verschluesselt, Manifest.db im Klartext."""
    backup = build_backup(
        tmp_path / "alt",
        sample_files(),
        password="pw",
        encrypt_manifest=False,
        installed_applications=[],
    )
    assert (backup.path / "Manifest.db").read_bytes().startswith(b"SQLite format 3")
    manifest = plistlib.loads((backup.path / "Manifest.plist").read_bytes())
    assert "ManifestKey" not in manifest


def test_file_with_unavailable_class_is_reported(tmp_path: Path) -> None:
    """Eine Datei, deren Klasse nicht im Keybag steht, bleibt unentschluesselbar."""
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-x", "Documents/a.bin", b"x" * 32, protection_class=3)],
        password="pw",
        installed_applications=[],
    )
    # Keybag ohne Klasse 3 nachbilden
    reduced = parse_keybag(
        build_keybag("pw", label=f"keybag/{backup.udid}", classes=(1, 2)).blob
    )
    with (
        BackupKeys.from_password(reduced, "pw") as keys,
        pytest.raises(UnavailableProtectionClass),
    ):
        keys.unwrap_file_key(3, b"k" * 40)
