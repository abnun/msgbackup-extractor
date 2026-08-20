"""Generator fuer synthetische Apple-iPhone-Backups.

Tests duerfen niemals echte private Daten benoetigen. Dieser Builder erzeugt
Backups, die im Format echten Finder-Backups entsprechen - inklusive
NSKeyedArchiver-kodierter MBFile-Blobs, echtem TLV-Keybag, echter
PBKDF2-Ableitung, echtem AES-Key-Wrap und echter AES-256-CBC-Verschluesselung.

Nur so beweisen die Tests etwas: Ein vereinfachtes Format wuerde den
Produktionscode nicht auf die Probe stellen.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

BLOCK_SIZE: Final = 16

#: Absichtlich niedrig, damit die Testsuite schnell bleibt. Echte Backups
#: verwenden deutlich hoehere Werte.
TEST_ITERATIONS: Final = 1000

#: Protection Classes, fuer die der Builder Schluessel in den Keybag legt.
DEFAULT_CLASSES: Final = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


# ---------------------------------------------------------------------------
# Deterministische Pseudo-Zufallswerte
# ---------------------------------------------------------------------------


def _deterministic_bytes(label: str, length: int) -> bytes:
    """Reproduzierbare Bytes fuer Salts, UUIDs und Schluessel.

    Bewusst nicht `os.urandom`: identische Fixtures ergeben identische Backups,
    was Fehlersuche und Vergleichbarkeit der Tests erheblich erleichtert.
    Fuer Testdaten ist das unbedenklich - echte Schluessel entstehen hier nie.
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"{label}/{counter}".encode()).digest()
        counter += 1
    return bytes(out[:length])


# ---------------------------------------------------------------------------
# Dateibeschreibung
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackupFile:
    """Eine Datei, die im synthetischen Backup landen soll."""

    domain: str
    relative_path: str
    content: bytes = b""
    protection_class: int = 3
    mode: int = 0o100644
    #: 1 = Datei, 2 = Verzeichnis, 4 = Symlink
    flags: int = 1
    last_modified: datetime | None = None
    birth: datetime | None = None
    #: Wenn True, wird der Manifest-Eintrag erzeugt, die Nutzdatei aber nicht.
    omit_payload: bool = False
    #: Wenn gesetzt, wird die geschriebene Nutzdatei nach n Bytes abgeschnitten.
    truncate_payload_to: int | None = None
    #: Wenn True, enthaelt der MBFile-Blob Muell statt einer gueltigen Struktur.
    corrupt_metadata: bool = False

    @property
    def file_id(self) -> str:
        """SHA-1 von "<domain>-<relativePath>", wie von iOS vergeben."""
        return hashlib.sha1(f"{self.domain}-{self.relative_path}".encode()).hexdigest()

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class BuiltBackup:
    """Ergebnis eines Builder-Laufs, inklusive der Erwartungswerte fuer Tests."""

    path: Path
    udid: str
    is_encrypted: bool
    password: str | None
    files: tuple[BackupFile, ...]
    #: file_id -> SHA-256 des Klartextinhalts
    expected_hashes: dict[str, str] = field(default_factory=dict)

    def file_by_path(self, relative_path: str) -> BackupFile:
        for f in self.files:
            if f.relative_path == relative_path:
                return f
        raise KeyError(relative_path)

    @property
    def payload_files(self) -> tuple[BackupFile, ...]:
        """Alle Eintraege, die tatsaechlich eine Nutzdatei auf der Platte haben."""
        return tuple(f for f in self.files if f.flags == 1 and not f.omit_payload)


# ---------------------------------------------------------------------------
# Keybag
# ---------------------------------------------------------------------------


def _tlv(tag: str, value: bytes | int) -> bytes:
    """Ein TLV-Element: 4-Byte-ASCII-Tag, 4-Byte-Laenge big-endian, Value."""
    payload = struct.pack(">I", value) if isinstance(value, int) else value
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


def derive_passcode_key(password: str, salt: bytes, iterations: int, dpsl: bytes, dpic: int) -> bytes:
    """Doppeltes PBKDF2 wie bei Keybag-Version >= 3.

    inner = PBKDF2-HMAC-SHA1  (password, DPSL, DPIC, 32)
    key   = PBKDF2-HMAC-SHA256(inner,    SALT, ITER, 32)
    """
    inner = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=dpsl, iterations=dpic).derive(
        password.encode("utf-8")
    )
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(inner)


@dataclass(slots=True)
class KeybagFixture:
    """Ein erzeugter Keybag samt der Klassenschluessel im Klartext."""

    blob: bytes
    passcode_key: bytes
    class_keys: dict[int, bytes]


def build_keybag(
    password: str,
    *,
    label: str = "keybag",
    classes: tuple[int, ...] = DEFAULT_CLASSES,
    iterations: int = TEST_ITERATIONS,
    version: int = 4,
) -> KeybagFixture:
    """Erzeugt einen realistischen Backup-Keybag mit echtem AES-Key-Wrap."""
    salt = _deterministic_bytes(f"{label}/salt", 20)
    dpsl = _deterministic_bytes(f"{label}/dpsl", 20)
    dpic = iterations
    passcode_key = derive_passcode_key(password, salt, iterations, dpsl, dpic)

    header = (
        _tlv("VERS", version)
        + _tlv("TYPE", 1)  # 1 = Backup-Keybag
        + _tlv("UUID", _deterministic_bytes(f"{label}/uuid", 16))
        + _tlv("HMCK", _deterministic_bytes(f"{label}/hmck", 40))
        + _tlv("WRAP", 1)
        + _tlv("SALT", salt)
        + _tlv("ITER", iterations)
        + _tlv("DPSL", dpsl)
        + _tlv("DPIC", dpic)
    )

    class_keys: dict[int, bytes] = {}
    body = b""
    for klass in classes:
        class_key = _deterministic_bytes(f"{label}/class/{klass}", 32)
        class_keys[klass] = class_key
        body += (
            _tlv("UUID", _deterministic_bytes(f"{label}/class-uuid/{klass}", 16))
            + _tlv("CLAS", klass)
            + _tlv("WRAP", 2)
            + _tlv("KTYP", 0)
            + _tlv("WPKY", aes_key_wrap(passcode_key, class_key))
        )

    return KeybagFixture(blob=header + body, passcode_key=passcode_key, class_keys=class_keys)


# ---------------------------------------------------------------------------
# Krypto-Hilfen
# ---------------------------------------------------------------------------


def aes_cbc_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-CBC mit Null-IV und PKCS7-Padding.

    Der Produktionscode kuerzt beim Entschluesseln auf die im Manifest
    vermerkte `Size` und ist damit unabhaengig von der Padding-Variante. Hier
    wird absichtlich PKCS7 verwendet, weil das der striktere Fall ist.
    """
    pad = BLOCK_SIZE - (len(plaintext) % BLOCK_SIZE)
    padded = plaintext + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * BLOCK_SIZE)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def wrapped_key_blob(protection_class: int, class_key: bytes, file_key: bytes) -> bytes:
    """`EncryptionKey`-Blob: 4 Byte Klasse (little-endian) + 40 Byte Wrapped Key."""
    return struct.pack("<I", protection_class) + aes_key_wrap(class_key, file_key)


# ---------------------------------------------------------------------------
# MBFile-Blob (NSKeyedArchiver)
# ---------------------------------------------------------------------------


def _apple_epoch(value: datetime | None) -> int:
    """Sekunden seit 2001-01-01, wie in MBFile-Zeitstempeln."""
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    reference = datetime(2001, 1, 1, tzinfo=UTC)
    return int((value.astimezone(UTC) - reference).total_seconds())


def build_mbfile_blob(entry: BackupFile, encryption_key: bytes | None) -> bytes:
    """Baut ein echtes NSKeyedArchiver-Plist fuer einen MBFile-Eintrag.

    Struktur wie in echten Backups: `$top.root` zeigt in `$objects` auf das
    MBFile-Dictionary; `EncryptionKey` ist eine UID-Referenz auf ein
    NSMutableData-Objekt, dessen `NS.data` mit 4 Byte Protection Class beginnt.
    """
    if entry.corrupt_metadata:
        return b"bplist00\xff\xfe kein gueltiges NSKeyedArchiver-Plist"

    objects: list[object] = ["$null"]

    file_dict: dict[str, object] = {}
    objects.append(file_dict)  # UID 1

    objects.append(entry.relative_path)
    relative_path_uid = plistlib.UID(len(objects) - 1)

    encryption_key_uid: plistlib.UID | None = None
    if encryption_key is not None:
        objects.append({"NS.data": encryption_key, "$class": plistlib.UID(0)})
        encryption_key_uid = plistlib.UID(len(objects) - 1)

    objects.append({"$classes": ["NSMutableData", "NSData", "NSObject"], "$classname": "NSMutableData"})
    nsdata_class_uid = plistlib.UID(len(objects) - 1)
    if encryption_key_uid is not None:
        objects[encryption_key_uid.data]["$class"] = nsdata_class_uid  # type: ignore[index]

    objects.append({"$classes": ["MBFile", "NSObject"], "$classname": "MBFile"})
    mbfile_class_uid = plistlib.UID(len(objects) - 1)

    file_dict.update(
        {
            "$class": mbfile_class_uid,
            "RelativePath": relative_path_uid,
            "Flags": entry.flags,
            "Mode": entry.mode,
            "Size": entry.size,
            "ProtectionClass": entry.protection_class,
            "UserID": 501,
            "GroupID": 501,
            "InodeNumber": 100000 + (int(entry.file_id[:6], 16) % 900000),
            "Birth": _apple_epoch(entry.birth or entry.last_modified),
            "LastModified": _apple_epoch(entry.last_modified),
            "LastStatusChange": _apple_epoch(entry.last_modified),
        }
    )
    if encryption_key_uid is not None:
        file_dict["EncryptionKey"] = encryption_key_uid

    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
        sort_keys=False,
    )


# ---------------------------------------------------------------------------
# Manifest.db
# ---------------------------------------------------------------------------

#: Schema wie in echten Backups ab iOS 10.
STANDARD_FILES_SCHEMA: Final = """
CREATE TABLE Files (
    fileID TEXT PRIMARY KEY,
    domain TEXT,
    relativePath TEXT,
    flags INTEGER,
    file BLOB
)
"""

#: Variante mit zusaetzlichen Spalten - der Produktionscode muss damit
#: umgehen, ohne sich auf eine feste Spaltenreihenfolge zu verlassen.
EXTENDED_FILES_SCHEMA: Final = """
CREATE TABLE Files (
    fileID TEXT PRIMARY KEY,
    domain TEXT,
    relativePath TEXT,
    flags INTEGER,
    file BLOB,
    extraColumn TEXT
)
"""

#: Voellig unbekanntes Schema - muss zum Diagnosebericht fuehren, nicht zu
#: geratenen Ergebnissen.
UNKNOWN_SCHEMA: Final = """
CREATE TABLE SomethingElse (
    id INTEGER PRIMARY KEY,
    payload BLOB
)
"""


def _write_manifest_db(
    path: Path,
    entries: list[tuple[str, str, str, int, bytes]],
    *,
    schema: str = STANDARD_FILES_SCHEMA,
) -> None:
    """Schreibt eine Manifest.db. Nutzt WAL nicht, damit nur eine Datei entsteht."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(schema)
        # Echte Backups haben zusaetzlich eine Properties-Tabelle.
        connection.execute("CREATE TABLE Properties (key TEXT PRIMARY KEY, value BLOB)")
        connection.execute(
            "INSERT INTO Properties (key, value) VALUES (?, ?)", ("salt", b"\x00" * 8)
        )
        if "CREATE TABLE Files" in schema:
            connection.executemany(
                "INSERT INTO Files (fileID, domain, relativePath, flags, file) "
                "VALUES (?, ?, ?, ?, ?)",
                entries,
            )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_backup(
    destination: Path,
    files: list[BackupFile],
    *,
    udid: str = "00008030-001A2B3C4D5E6F70",
    password: str | None = None,
    installed_applications: list[str] | None = None,
    application_versions: dict[str, str] | None = None,
    device_name: str = "Test iPhone",
    product_version: str = "17.5.1",
    product_type: str = "iPhone15,2",
    schema: str = STANDARD_FILES_SCHEMA,
    keybag_version: int = 4,
    iterations: int = TEST_ITERATIONS,
    encrypt_manifest: bool = True,
    omit_manifest_key: bool = False,
) -> BuiltBackup:
    """Erzeugt ein vollstaendiges synthetisches Backup unter `destination/udid`.

    `password is None` ergibt ein unverschluesseltes Backup. Andernfalls werden
    Keybag, Manifest.db und alle Nutzdateien echt verschluesselt.

    Args:
        encrypt_manifest: Nur relevant bei verschluesseltem Backup. False
            simuliert aeltere Backups, bei denen Manifest.db im Klartext liegt.
        omit_manifest_key: Laesst `ManifestKey` in Manifest.plist weg, obwohl
            das Backup verschluesselt ist - ein Fehlerfall fuer die Diagnose.
    """
    is_encrypted = password is not None
    backup_path = destination / udid
    backup_path.mkdir(parents=True, exist_ok=True)

    keybag: KeybagFixture | None = None
    if is_encrypted:
        assert password is not None
        keybag = build_keybag(
            password, label=f"keybag/{udid}", iterations=iterations, version=keybag_version
        )

    entries: list[tuple[str, str, str, int, bytes]] = []
    expected_hashes: dict[str, str] = {}

    for entry in files:
        encryption_key_blob: bytes | None = None
        payload = entry.content

        if entry.flags == 1:
            expected_hashes[entry.file_id] = hashlib.sha256(entry.content).hexdigest()

        if is_encrypted and entry.flags == 1:
            assert keybag is not None
            class_key = keybag.class_keys.get(entry.protection_class)
            if class_key is None:
                raise ValueError(
                    f"Protection Class {entry.protection_class} ist nicht im Keybag; "
                    "fuer diesen Fall `classes=` beim Keybag anpassen"
                )
            file_key = _deterministic_bytes(f"filekey/{entry.file_id}", 32)
            encryption_key_blob = wrapped_key_blob(entry.protection_class, class_key, file_key)
            payload = aes_cbc_encrypt(file_key, entry.content)

        entries.append(
            (
                entry.file_id,
                entry.domain,
                entry.relative_path,
                entry.flags,
                build_mbfile_blob(entry, encryption_key_blob),
            )
        )

        if entry.flags != 1 or entry.omit_payload:
            continue
        if entry.truncate_payload_to is not None:
            payload = payload[: entry.truncate_payload_to]
        target = backup_path / entry.file_id[:2] / entry.file_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    # -- Manifest.db --------------------------------------------------------
    manifest_path = backup_path / "Manifest.db"
    _write_manifest_db(manifest_path, entries, schema=schema)

    manifest_key_blob: bytes | None = None
    if is_encrypted and encrypt_manifest:
        assert keybag is not None
        manifest_class = 4
        manifest_file_key = _deterministic_bytes(f"manifestkey/{udid}", 32)
        manifest_key_blob = wrapped_key_blob(
            manifest_class, keybag.class_keys[manifest_class], manifest_file_key
        )
        manifest_path.write_bytes(aes_cbc_encrypt(manifest_file_key, manifest_path.read_bytes()))

    # -- Manifest.plist -----------------------------------------------------
    bundle_ids = installed_applications or []
    versions = application_versions or {}
    applications = {
        bundle_id: {
            "CFBundleIdentifier": bundle_id,
            "CFBundleVersion": versions.get(bundle_id, "1.0"),
            "PlaceholderIcon": b"",
        }
        for bundle_id in bundle_ids
    }
    # plistlib schreibt im Binaerformat nur naive Datetimes; sie gelten als UTC.
    backup_date = datetime(2026, 8, 20, 9, 30, 0)

    manifest_plist: dict[str, object] = {
        "Version": "10.0",
        "Date": backup_date,
        "SystemDomainsVersion": "26.0",
        "WasPasscodeSet": is_encrypted,
        "IsEncrypted": is_encrypted,
        "Applications": applications,
        "Lockdown": {"ProductVersion": product_version, "ProductType": product_type},
    }
    if is_encrypted:
        assert keybag is not None
        manifest_plist["BackupKeyBag"] = keybag.blob
        if manifest_key_blob is not None and not omit_manifest_key:
            manifest_plist["ManifestKey"] = manifest_key_blob
    (backup_path / "Manifest.plist").write_bytes(
        plistlib.dumps(manifest_plist, fmt=plistlib.FMT_BINARY)
    )

    # -- Info.plist ---------------------------------------------------------
    info_plist: dict[str, object] = {
        "Device Name": device_name,
        "Display Name": device_name,
        "Product Name": "iPhone",
        "Product Type": product_type,
        "Product Version": product_version,
        "Build Version": "21F90",
        "Unique Identifier": udid.replace("-", ""),
        "Serial Number": "TESTSERIAL01",
        "IMEI": "000000000000000",
        "Last Backup Date": backup_date,
        "iTunes Version": "12.13",
        "Installed Applications": bundle_ids,
        "Applications": applications,
    }
    (backup_path / "Info.plist").write_bytes(plistlib.dumps(info_plist, fmt=plistlib.FMT_XML))

    # -- Status.plist -------------------------------------------------------
    (backup_path / "Status.plist").write_bytes(
        plistlib.dumps(
            {
                "BackupState": "new",
                "Date": backup_date,
                "IsFullBackup": True,
                "SnapshotState": "finished",
                "UUID": udid,
                "Version": "3.3",
            },
            fmt=plistlib.FMT_BINARY,
        )
    )

    return BuiltBackup(
        path=backup_path,
        udid=udid,
        is_encrypted=is_encrypted,
        password=password,
        files=tuple(files),
        expected_hashes=expected_hashes,
    )
