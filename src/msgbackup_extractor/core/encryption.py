"""Entschluesselung eines verschluesselten Apple-Backups.

**Keine eigene Kryptografie.** Alle Primitive kommen aus `cryptography` (PyCA):

* `PBKDF2HMAC` fuer die Schluesselableitung aus dem Passwort
* `keywrap.aes_key_unwrap` fuer AES-Key-Wrap nach RFC 3394
* `Cipher(AES, CBC)` fuer die Dateientschluesselung

Eigener Code beschraenkt sich auf die Reihenfolge der Schritte und auf das
Zusammensetzen der Werte aus dem Keybag - also auf Formatlogik.

Ablauf:

1. Passcode-Key aus dem Passwort ableiten (Salt und Iterationen aus dem Keybag).
2. Klassenschluessel mit AES-Key-Wrap entpacken. Die Integritaetspruefung des
   Key-Wrap ist gleichzeitig die Passwortpruefung: schlaegt sie fuer alle
   Klassen fehl, war das Passwort falsch.
3. Pro Datei den Dateischluessel mit dem Klassenschluessel entpacken.
4. Datei mit AES-256-CBC und Null-IV entschluesseln, dann auf die im Manifest
   vermerkte Groesse kuerzen.

Schritt 4 kuerzt bewusst auf `Size`, statt eine bestimmte Padding-Variante
anzunehmen. Das ist unabhaengig davon, ob iOS mit Nullen oder nach PKCS7
auffuellt.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Self

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, CipherContext, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

from msgbackup_extractor.core.keybag import DOUBLE_PBKDF_VERSION, Keybag
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.secure_memory import SecretBytes

logger = get_logger("encryption")

BLOCK_SIZE: Final = 16

#: Null-IV, wie von iOS fuer Backupdateien verwendet.
ZERO_IV: Final = b"\x00" * BLOCK_SIZE

#: Laenge des abgeleiteten Passcode-Keys.
DERIVED_KEY_LENGTH: Final = 32

#: Ein `EncryptionKey`-Blob beginnt mit 4 Byte Protection Class (little-endian).
CLASS_PREFIX_LENGTH: Final = 4

#: Blockgroesse fuer streamende Entschluesselung.
CHUNK_SIZE: Final = 1024 * 1024


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------


class DecryptionError(RuntimeError):
    """Entschluesselung nicht moeglich."""


class WrongPasswordError(DecryptionError):
    """Das eingegebene Passwort ist falsch.

    Erkannt an der Integritaetspruefung von AES-Key-Wrap: mit einem falschen
    Passcode-Key laesst sich kein einziger Klassenschluessel entpacken. Es wird
    also nie stillschweigend Datenmuell erzeugt.
    """

    def __init__(self) -> None:
        super().__init__(
            "Das Passwort ist falsch. Es ist das Passwort, das beim Einrichten "
            "der Backup-Verschluesselung im Finder gesetzt wurde - nicht der "
            "Gerätecode des iPhones und nicht das Apple-ID-Passwort."
        )


class UnavailableProtectionClass(DecryptionError):
    """Fuer diese Protection Class liegt kein Schluessel vor."""

    def __init__(self, protection_class: int, available: tuple[int, ...]) -> None:
        self.protection_class = protection_class
        self.available = available
        super().__init__(
            f"Fuer Protection Class {protection_class} liegt kein Schluessel vor. "
            f"Verfuegbar sind: {', '.join(map(str, available)) or '(keine)'}. "
            "Diese Datei bleibt unentschluesselbar."
        )


class UndecryptableFile(DecryptionError):
    """Die Datei laesst sich nicht entschluesseln."""


class TruncatedFile(DecryptionError):
    """Die Datei ist kuerzer als im Manifest vermerkt.

    Eigene Klasse, weil dieser Fall anders zu behandeln ist als eine
    unentschluesselbare Datei: die Daten sind teilweise wiederherstellbar, aber
    unvollstaendig. Das muss im Bericht stehen und darf nicht wie ein Erfolg
    aussehen.
    """

    def __init__(self, name: str, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        missing = expected - actual
        super().__init__(
            f"Die Datei ist unvollstaendig: erwartet wurden {expected} Byte, "
            f"entschluesselt wurden {actual} ({missing} Byte fehlen). Die "
            "Quelldatei im Backup ist abgeschnitten."
        )


# ---------------------------------------------------------------------------
# Schluesselableitung
# ---------------------------------------------------------------------------


def derive_passcode_key(password: str, keybag: Keybag) -> SecretBytes:
    """Leitet den Passcode-Key aus dem Passwort ab.

    Ab Keybag-Version 3 verwendet iOS eine doppelte Ableitung:

        inner = PBKDF2-HMAC-SHA1  (password, DPSL, DPIC, 32)
        key   = PBKDF2-HMAC-SHA256(inner,    SALT, ITER, 32)

    Aeltere Keybags verwenden ein einfaches PBKDF2-HMAC-SHA1.

    Das Ergebnis liegt in einem `SecretBytes` und sollte nach Gebrauch gewipet
    werden; `BackupKeys.from_password()` erledigt das.
    """
    encoded = password.encode("utf-8")

    if keybag.uses_double_derivation:
        assert keybag.double_salt is not None
        assert keybag.double_iterations is not None
        inner = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=DERIVED_KEY_LENGTH,
            salt=keybag.double_salt,
            iterations=keybag.double_iterations,
        ).derive(encoded)
        derived = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=DERIVED_KEY_LENGTH,
            salt=keybag.salt,
            iterations=keybag.iterations,
        ).derive(inner)
        return SecretBytes(derived)

    if keybag.version >= DOUBLE_PBKDF_VERSION:
        logger.warning(
            "Keybag-Version %d ohne DPSL/DPIC: es wird die einfache Ableitung versucht.",
            keybag.version,
        )
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=DERIVED_KEY_LENGTH,
        salt=keybag.salt,
        iterations=keybag.iterations,
    ).derive(encoded)
    return SecretBytes(derived)


# ---------------------------------------------------------------------------
# Schluesselverwaltung
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackupKeys:
    """Die entpackten Klassenschluessel eines Backups.

    Als Context Manager verwenden, damit die Schluessel am Ende ueberschrieben
    werden:

        with BackupKeys.from_password(keybag, password) as keys:
            ...
    """

    class_keys: dict[int, SecretBytes]
    #: Klassen, deren Schluessel im Keybag standen, aber nicht entpackbar waren.
    unavailable_classes: tuple[int, ...] = ()

    # -- Erzeugung ----------------------------------------------------------

    @classmethod
    def from_password(cls, keybag: Keybag, password: str) -> BackupKeys:
        """Leitet den Passcode-Key ab und entpackt alle Klassenschluessel.

        Raises:
            WrongPasswordError: Wenn kein einziger Klassenschluessel entpackt
                werden konnte.
        """
        unwrapped: dict[int, SecretBytes] = {}
        failed: list[int] = []

        with derive_passcode_key(password, keybag) as passcode_key:
            key_bytes = passcode_key.reveal()
            for protection_class, class_key in sorted(keybag.class_keys.items()):
                try:
                    unwrapped[protection_class] = SecretBytes(
                        aes_key_unwrap(key_bytes, class_key.wrapped_key)
                    )
                except (InvalidUnwrap, InvalidKey, ValueError):
                    failed.append(protection_class)

        if not unwrapped:
            # Alle Klassen fehlgeschlagen: das ist das Passwort, nicht das Backup.
            raise WrongPasswordError

        if failed:
            logger.warning(
                "%d von %d Klassenschluesseln liessen sich nicht entpacken "
                "(Klassen: %s). Betroffene Dateien bleiben unentschluesselbar.",
                len(failed),
                len(keybag.class_keys),
                ", ".join(map(str, failed)),
            )

        logger.debug("%d Klassenschluessel entpackt", len(unwrapped))
        return cls(class_keys=unwrapped, unavailable_classes=tuple(failed))

    # -- Zugriff ------------------------------------------------------------

    @property
    def available_classes(self) -> tuple[int, ...]:
        return tuple(sorted(self.class_keys))

    def has_class(self, protection_class: int) -> bool:
        return protection_class in self.class_keys

    def unwrap_file_key(self, protection_class: int, wrapped_key: bytes) -> SecretBytes:
        """Entpackt den Schluessel einer einzelnen Datei.

        Raises:
            UnavailableProtectionClass: Wenn der Klassenschluessel fehlt.
            UndecryptableFile: Wenn der Wrapped Key nicht zum Klassenschluessel passt.
        """
        class_key = self.class_keys.get(protection_class)
        if class_key is None:
            raise UnavailableProtectionClass(protection_class, self.available_classes)
        try:
            return SecretBytes(aes_key_unwrap(class_key.reveal(), wrapped_key))
        except (InvalidUnwrap, InvalidKey, ValueError) as error:
            raise UndecryptableFile(
                f"Der Dateischluessel passt nicht zum Schluessel der Protection "
                f"Class {protection_class}: {type(error).__name__}"
            ) from error

    # -- Lebenszyklus -------------------------------------------------------

    def wipe(self) -> None:
        """Ueberschreibt alle Klassenschluessel."""
        for secret in self.class_keys.values():
            secret.wipe()
        self.class_keys = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.wipe()

    def __repr__(self) -> str:
        return (
            f"BackupKeys(classes={self.available_classes}, "
            f"unavailable={self.unavailable_classes})"
        )


# ---------------------------------------------------------------------------
# Dateientschluesselung
# ---------------------------------------------------------------------------


def split_encryption_key_blob(blob: bytes) -> tuple[int, bytes]:
    """Trennt einen `EncryptionKey`-Blob in Protection Class und Wrapped Key.

    Der Blob beginnt mit vier Byte Protection Class (little-endian). Die Klasse
    ist redundant zum Feld `ProtectionClass` des MBFile-Eintrags; sie wird
    trotzdem zurueckgegeben, damit ein Widerspruch auffallen kann.
    """
    if len(blob) <= CLASS_PREFIX_LENGTH:
        raise UndecryptableFile(f"EncryptionKey-Blob zu kurz: {len(blob)} Byte")
    protection_class = int.from_bytes(blob[:CLASS_PREFIX_LENGTH], "little")
    return protection_class, blob[CLASS_PREFIX_LENGTH:]


def _decryptor(key: bytes) -> CipherContext:
    return Cipher(algorithms.AES(key), modes.CBC(ZERO_IV)).decryptor()


def decrypt_bytes(data: bytes, key: SecretBytes, *, size: int | None = None) -> bytes:
    """Entschluesselt Daten vollstaendig im Speicher. Nur fuer kleine Dateien."""
    if len(data) % BLOCK_SIZE:
        raise UndecryptableFile(
            f"Die verschluesselte Datei ist {len(data)} Byte lang und damit kein "
            f"Vielfaches der Blockgroesse {BLOCK_SIZE}. Sie ist beschaedigt oder "
            "abgeschnitten."
        )
    decryptor = _decryptor(key.reveal())
    plaintext = decryptor.update(data) + decryptor.finalize()
    return plaintext if size is None else plaintext[:size]


def decrypt_stream(
    source: BinaryIO,
    key: SecretBytes,
    *,
    size: int | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> Iterator[bytes]:
    """Entschluesselt streamend und kuerzt auf `size`.

    Grosse Videos werden so nie vollstaendig in den Speicher geladen. Der
    Iterator muss erschoepft werden, sonst bleibt der Cipher-Kontext offen.
    """
    decryptor = _decryptor(key.reveal())
    emitted = 0
    # Blockgroesse ausrichten, damit `update` nicht unnoetig puffert.
    aligned = max(BLOCK_SIZE, (chunk_size // BLOCK_SIZE) * BLOCK_SIZE)

    while True:
        encrypted = source.read(aligned)
        if not encrypted:
            break
        plaintext = decryptor.update(encrypted)
        if not plaintext:
            continue
        if size is not None:
            remaining = size - emitted
            if remaining <= 0:
                break
            plaintext = plaintext[:remaining]
        emitted += len(plaintext)
        yield plaintext

    try:
        tail = decryptor.finalize()
    except ValueError as error:
        raise UndecryptableFile(
            "Die verschluesselte Datei endet nicht auf einer Blockgrenze; sie "
            "ist beschaedigt oder abgeschnitten."
        ) from error

    if tail:
        if size is not None:
            tail = tail[: max(0, size - emitted)]
        if tail:
            yield tail


def decrypt_head(source_path: Path, key: SecretBytes, length: int) -> bytes:
    """Entschluesselt nur den Anfang einer Datei.

    Bei AES-CBC haengt ein Klartextblock nur vom eigenen und vom vorherigen
    Chiffratblock ab. Der Dateianfang laesst sich deshalb entschluesseln, ohne
    die ganze Datei zu lesen - genau das braucht die Signaturerkennung bei
    grossen Videos.
    """
    aligned = ((length + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
    with source_path.open("rb") as handle:
        encrypted = handle.read(aligned)
    usable = (len(encrypted) // BLOCK_SIZE) * BLOCK_SIZE
    if not usable:
        return b""
    decryptor = _decryptor(key.reveal())
    return decryptor.update(encrypted[:usable])[:length]


def decrypt_file_to(
    source_path: Path,
    destination_path: Path,
    key: SecretBytes,
    *,
    size: int | None = None,
    require_full_size: bool = True,
) -> int:
    """Entschluesselt eine Datei in eine neue Datei. Gibt die Anzahl Bytes zurueck.

    Die Quelle wird ausschliesslich lesend geoeffnet. Das Zielverzeichnis muss
    bereits existieren - dafuer ist der Output-Guard zustaendig.

    Args:
        size: Die im Manifest vermerkte Klartextgroesse.
        require_full_size: Wenn True (Standard) und weniger Bytes herauskommen
            als `size` angibt, wird das als Fehler gemeldet.

    Warum die Pruefung noetig ist: eine abgeschnittene Datei, deren Laenge
    zufaellig ein Vielfaches der Blockgroesse ist, entschluesselt ohne
    Beanstandung - nur eben zu weniger Daten. Ohne Vergleich mit `size` waere
    das ein stiller Teilverlust, der wie ein Erfolg aussieht.
    """
    written = 0
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        for chunk in decrypt_stream(source, key, size=size):
            destination.write(chunk)
            written += len(chunk)

    if require_full_size and size is not None and written != size:
        destination_path.unlink(missing_ok=True)
        raise TruncatedFile(source_path.name, expected=size, actual=written)

    return written


def decrypt_manifest_database(
    encrypted_path: Path,
    destination_path: Path,
    keys: BackupKeys,
    manifest_key_blob: bytes,
) -> Path:
    """Entschluesselt Manifest.db in eine Datei ausserhalb des Backups.

    Args:
        encrypted_path: Die verschluesselte Manifest.db im Backup (nur lesend).
        destination_path: Ziel ausserhalb des Backups.
        keys: Die entpackten Klassenschluessel.
        manifest_key_blob: `Manifest.plist:ManifestKey`.

    Raises:
        UndecryptableFile: Wenn das Ergebnis keine SQLite-Datenbank ist.
    """
    protection_class, wrapped = split_encryption_key_blob(manifest_key_blob)
    with keys.unwrap_file_key(protection_class, wrapped) as manifest_key:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        decrypt_file_to(encrypted_path, destination_path, manifest_key)

    with destination_path.open("rb") as handle:
        magic = handle.read(16)
    if magic != b"SQLite format 3\x00":
        destination_path.unlink(missing_ok=True)
        raise UndecryptableFile(
            "Die entschluesselte Manifest.db ist keine SQLite-Datenbank. Das "
            "deutet auf einen falschen ManifestKey oder ein beschaedigtes "
            "Backup hin."
        )

    logger.debug("Manifest.db entschluesselt (Protection Class %d)", protection_class)
    return destination_path
