"""Erzeugt einen synthetischen Threema-Core-Data-Store.

Nachgebildet wird die Struktur, die am echten Backup vermessen wurde (siehe
Design-Dokument §19). Entscheidend sind vier Eigenschaften, ohne die die Tests
nichts beweisen wuerden:

1. **Zwei Quellen.** Manche Blobs sind Referenzen auf `_EXTERNAL_DATA`, andere
   liegen inline in der Datenbank. Format der Referenz: `0x02` + 36 Byte UUID
   als ASCII + `0x00`.
2. **Verwaiste Rueckrichtung.** `ZIMAGEDATA.ZMESSAGE` wird absichtlich mit
   Werten gefuellt, die auf keine Nachricht zeigen - genau wie im echten
   Backup. Wer dort joint, bekommt null Treffer.
3. **Beziehungen auf der Nachrichtenseite.** `ZMESSAGE.ZTHUMBNAIL`, `ZIMAGE`,
   `ZVIDEO`, `ZAUDIO` und `ZDATA` zeigen auf die jeweilige `*DATA`-Tabelle.
4. **Luecken.** Eintraege ohne Chatbezug, ohne Dateinamen und ohne Zeitstempel,
   damit die Fallbacks getestet werden statt nur der Gutfall.

Keine dieser Daten hat einen personenbezogenen Ursprung; alle Namen sind
erfunden.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from tests.support.backup_builder import BackupFile

THREEMA_BUNDLE_ID: Final = "ch.threema.iapp"
APP_DOMAIN: Final = f"AppDomain-{THREEMA_BUNDLE_ID}"
GROUP_DOMAIN: Final = "AppDomainGroup-group.ch.threema"
PLUGIN_DOMAIN: Final = f"AppDomainPlugin-{THREEMA_BUNDLE_ID}.ThreemaShareExtension"

DATABASE_NAME: Final = "ThreemaData.sqlite"
EXTERNAL_DIR: Final = ".ThreemaData_SUPPORT/_EXTERNAL_DATA"

#: Praefix und Suffix einer Referenz auf eine externe Blob-Datei.
REF_PREFIX: Final = b"\x02"
REF_SUFFIX: Final = b"\x00"

#: Markierungsbyte fuer inline abgelegte Daten.
INLINE_MARKER: Final = 0x01

APPLE_EPOCH: Final = datetime(2001, 1, 1, tzinfo=UTC)

#: Erwartete Anzeigenamen der Konversationen im Fixture, nach Z_PK.
#: Gruppen ueber ZGROUPNAME, Einzelchats ueber Vor-/Nachname, sonst Nickname,
#: sonst die Threema-ID.
EXPECTED_CHAT_NAMES: Final[dict[int, str]] = {
    1: "Familie",
    2: "Max Mustermann",
    3: "nordlicht",
    4: "CCCC3333",
}

# Signaturen fuer die Medienerkennung
JPEG: Final = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG: Final = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
MP4: Final = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
M4A: Final = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"
PDF: Final = b"%PDF-1.7\n%\xc3\xa4\xc3\xb6\n"


def inline_blob(content: bytes) -> bytes:
    """Verpackt Daten so, wie Core Data sie inline ablegt.

    Das fuehrende `0x01` ist nicht Zierrat: es unterscheidet Inline-Daten von
    einer Referenz (`0x02`). Am echten Backup beginnen alle 5.973 Inline-Blobs
    damit. Ohne dieses Byte im Fixture wuerde kein Test bemerken, dass der
    Extractor es abschneiden muss.
    """
    return bytes([INLINE_MARKER]) + content


def external_reference(uuid: str) -> bytes:
    """Baut eine Referenz auf `_EXTERNAL_DATA/<uuid>` im echten Format."""
    if len(uuid) != 36:
        raise ValueError(f"UUID muss 36 Zeichen haben, hat {len(uuid)}")
    return REF_PREFIX + uuid.encode("ascii") + REF_SUFFIX


def _uuid(index: int) -> str:
    """Reproduzierbare, formal korrekte UUID in Grossbuchstaben."""
    hexed = f"{index:032X}"
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


def _timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - APPLE_EPOCH).total_seconds()


# ---------------------------------------------------------------------------
# Beschreibung des gewuenschten Inhalts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExpectedMedia:
    """Was der Extractor fuer ein Medium finden soll - die Testerwartung."""

    #: "external" oder "inline"
    storage: str
    #: Bei external der UUID-Dateiname, bei inline "<Tabelle>:<Z_PK>:ZDATA".
    identity: str
    content: bytes
    chat_name: str | None
    original_filename: str | None
    timestamp: datetime | None
    is_thumbnail: bool = False
    declared_mime: str | None = None


@dataclass(slots=True)
class ThreemaFixture:
    """Der erzeugte Store samt Erwartungswerten."""

    database: bytes
    #: UUID-Dateiname -> Inhalt, gehoert nach `_EXTERNAL_DATA`.
    external_blobs: dict[str, bytes] = field(default_factory=dict)
    expected: list[ExpectedMedia] = field(default_factory=list)

    def backup_files(self) -> list[BackupFile]:
        """Die Fixture als Liste von Backup-Dateien fuer `build_backup()`."""
        files = [
            BackupFile(GROUP_DOMAIN, DATABASE_NAME, self.database),
            BackupFile(GROUP_DOMAIN, EXTERNAL_DIR, b"", flags=2, mode=0o40755),
        ]
        files += [
            BackupFile(GROUP_DOMAIN, f"{EXTERNAL_DIR}/{uuid}", content)
            for uuid, content in sorted(self.external_blobs.items())
        ]
        # App-Interna, die nicht in die Medienverzeichnisse gehoeren.
        files += [
            BackupFile(APP_DOMAIN, "Library/Preferences/ch.threema.iapp.plist",
                       b"bplist00" + b"\x00" * 40),
            BackupFile(GROUP_DOMAIN, "Library/Application Support/.tipkit/tips-store.db",
                       b"SQLite format 3\x00" + b"\x00" * 60),
            BackupFile(GROUP_DOMAIN, "Documents/threema.log", b"2026-01-01 gestartet\n"),
            BackupFile(PLUGIN_DOMAIN, "Library/Preferences/share.plist", b"bplist00" + b"\x00" * 8),
        ]
        return files

    @property
    def expected_originals(self) -> list[ExpectedMedia]:
        return [e for e in self.expected if not e.is_thumbnail]

    @property
    def expected_thumbnails(self) -> list[ExpectedMedia]:
        return [e for e in self.expected if e.is_thumbnail]

    def by_identity(self, identity: str) -> ExpectedMedia:
        for candidate in self.expected:
            if candidate.identity == identity:
                return candidate
        raise KeyError(identity)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA: Final = """
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB);
CREATE TABLE Z_PRIMARYKEY (
    Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);

CREATE TABLE ZCONTACT (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZIDENTITY VARCHAR, ZFIRSTNAME VARCHAR, ZLASTNAME VARCHAR,
    ZPUBLICNICKNAME VARCHAR, ZPUBLICKEY BLOB);

CREATE TABLE ZCONVERSATION (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZCONTACT INTEGER, ZGROUPNAME VARCHAR, ZGROUPID BLOB, ZLASTUPDATE TIMESTAMP);

CREATE TABLE ZMESSAGE (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZCONVERSATION INTEGER, ZSENDER INTEGER,
    ZDATA INTEGER, ZIMAGE INTEGER, ZTHUMBNAIL INTEGER, ZTHUMBNAIL1 INTEGER,
    ZTHUMBNAIL2 INTEGER, ZVIDEO INTEGER, ZAUDIO INTEGER,
    ZFILENAME VARCHAR, ZMIMETYPE VARCHAR, ZDATE TIMESTAMP, ZTYPE INTEGER);

CREATE TABLE ZFILEDATA (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZMESSAGE INTEGER, ZDATA BLOB);

CREATE TABLE ZIMAGEDATA (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZWIDTH INTEGER, ZHEIGHT INTEGER, ZMESSAGE INTEGER, ZDATA BLOB);

CREATE TABLE ZVIDEODATA (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZMESSAGE INTEGER, ZDATA BLOB);

CREATE TABLE ZAUDIODATA (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZMESSAGE INTEGER, ZDATA BLOB);
"""

#: Entitaetsnummern wie in Z_PRIMARYKEY.
_ENTITIES: Final = {
    "ZCONTACT": 1,
    "ZCONVERSATION": 2,
    "ZMESSAGE": 14,
    "ZFILEDATA": 6,
    "ZIMAGEDATA": 8,
    "ZVIDEODATA": 20,
    "ZAUDIODATA": 3,
}


# ---------------------------------------------------------------------------
# Erzeugung
# ---------------------------------------------------------------------------


def build_threema_store() -> ThreemaFixture:
    """Baut einen Store, der alle relevanten Faelle abdeckt.

    Inhalt:

    * Gruppenchat "Familie" mit Bild (extern) samt Thumbnail (inline)
    * Einzelchat mit Vor- und Nachnamen: Dokument (extern), Sprachnachricht
      (inline, wie in aktuellen Threema-Versionen ueber ZFILEDATA)
    * Einzelchat nur mit Nickname: Video (extern)
    * Einzelchat ohne jeden Namen: Bild (extern) - Fallback auf die Chat-ID
    * Bild ohne Nachrichtenbezug -> `unassigned/`
    * Datei ohne ZFILENAME -> Fallback-Namensschema
    * Datei ohne ZDATE -> `unknown-date`
    * Referenz auf eine externe Datei, die im Backup fehlt
    """
    fixture = ThreemaFixture(database=b"")
    external: dict[str, bytes] = {}
    expected: list[ExpectedMedia] = []

    contacts = [
        # (Z_PK, Identity, Vorname, Nachname, Nickname)
        (1, "AAAA1111", "Max", "Mustermann", "maxi"),
        (2, "BBBB2222", None, None, "nordlicht"),
        (3, "CCCC3333", None, None, None),
    ]
    conversations = [
        # (Z_PK, ZCONTACT, ZGROUPNAME)
        (1, None, "Familie"),
        (2, 1, None),
        (3, 2, None),
        (4, 3, None),
    ]
    messages: list[dict[str, object]] = []
    file_rows: list[tuple[int, int | None, bytes]] = []
    image_rows: list[tuple[int, int | None, bytes, int, int]] = []
    video_rows: list[tuple[int, int | None, bytes]] = []
    audio_rows: list[tuple[int, int | None, bytes]] = []

    base = datetime(2025, 3, 14, 18, 42, 11, tzinfo=UTC)
    counter = {"uuid": 0, "message": 0, "file": 0, "image": 0, "video": 0, "audio": 0}

    def next_uuid() -> str:
        counter["uuid"] += 1
        return _uuid(counter["uuid"])

    def add_message(
        conversation: int | None,
        *,
        sender: int | None = None,
        filename: str | None = None,
        mime: str | None = None,
        when: datetime | None = None,
    ) -> int:
        counter["message"] += 1
        pk = counter["message"]
        messages.append(
            {
                "pk": pk,
                "conversation": conversation,
                "sender": sender,
                "filename": filename,
                "mime": mime,
                "date": when,
            }
        )
        return pk

    def link(pk: int, column: str, target: int) -> None:
        for message in messages:
            if message["pk"] == pk:
                message[column] = target
                return
        raise KeyError(pk)

    # -- Gruppenchat: Bild extern, Thumbnail inline -------------------------
    message = add_message(1, sender=1, filename="urlaub.jpg", mime="image/jpeg", when=base)
    uuid = next_uuid()
    external[uuid] = JPEG + b"A" * 900
    counter["image"] += 1
    image_pk = counter["image"]
    image_rows.append((image_pk, None, external_reference(uuid), 1920, 1080))
    link(message, "ZIMAGE", image_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "Familie", "urlaub.jpg", base,
                      declared_mime="image/jpeg")
    )

    counter["image"] += 1
    thumb_pk = counter["image"]
    thumb = JPEG + b"t" * 200
    image_rows.append((thumb_pk, None, inline_blob(thumb), 320, 180))
    link(message, "ZTHUMBNAIL", thumb_pk)
    expected.append(
        ExpectedMedia("inline", f"ZIMAGEDATA:{thumb_pk}:ZDATA", thumb, "Familie",
                      "urlaub.jpg", base, is_thumbnail=True, declared_mime="image/jpeg")
    )

    # -- Einzelchat mit Namen: Dokument extern ------------------------------
    message = add_message(2, sender=1, filename="Vertrag.pdf", mime="application/pdf",
                          when=base)
    uuid = next_uuid()
    external[uuid] = PDF + b"B" * 500
    counter["file"] += 1
    file_pk = counter["file"]
    file_rows.append((file_pk, message, external_reference(uuid)))
    link(message, "ZDATA", file_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "Max Mustermann", "Vertrag.pdf",
                      base, declared_mime="application/pdf")
    )

    # -- Sprachnachricht inline ueber ZFILEDATA -----------------------------
    message = add_message(2, sender=1, filename="sprachnachricht.m4a", mime="audio/mp4",
                          when=base)
    counter["file"] += 1
    file_pk = counter["file"]
    voice = M4A + b"C" * 300
    file_rows.append((file_pk, message, inline_blob(voice)))
    link(message, "ZDATA", file_pk)
    expected.append(
        ExpectedMedia("inline", f"ZFILEDATA:{file_pk}:ZDATA", voice, "Max Mustermann",
                      "sprachnachricht.m4a", base, declared_mime="audio/mp4")
    )

    # -- Einzelchat nur Nickname: Video extern ------------------------------
    message = add_message(3, sender=2, filename="clip.mp4", mime="video/mp4", when=base)
    uuid = next_uuid()
    external[uuid] = MP4 + b"D" * 1500
    counter["video"] += 1
    video_pk = counter["video"]
    video_rows.append((video_pk, None, external_reference(uuid)))
    link(message, "ZVIDEO", video_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "nordlicht", "clip.mp4", base,
                      declared_mime="video/mp4")
    )

    # -- Einzelchat ohne Namen: Bild extern (Fallback auf Chat-ID) ----------
    message = add_message(4, sender=3, filename="foto.png", mime="image/png", when=base)
    uuid = next_uuid()
    external[uuid] = PNG + b"E" * 400
    counter["file"] += 1
    file_pk = counter["file"]
    file_rows.append((file_pk, message, external_reference(uuid)))
    link(message, "ZDATA", file_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "CCCC3333", "foto.png", base,
                      declared_mime="image/png")
    )

    # -- Ohne Nachrichtenbezug -> unassigned -------------------------------
    uuid = next_uuid()
    external[uuid] = JPEG + b"F" * 250
    counter["file"] += 1
    file_rows.append((counter["file"], None, external_reference(uuid)))
    expected.append(ExpectedMedia("external", uuid, external[uuid], None, None, None))

    # -- Ohne Dateinamen ---------------------------------------------------
    message = add_message(1, sender=1, filename=None, mime="image/jpeg", when=base)
    uuid = next_uuid()
    external[uuid] = JPEG + b"G" * 350
    counter["file"] += 1
    file_pk = counter["file"]
    file_rows.append((file_pk, message, external_reference(uuid)))
    link(message, "ZDATA", file_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "Familie", None, base,
                      declared_mime="image/jpeg")
    )

    # -- Ohne Zeitstempel --------------------------------------------------
    message = add_message(1, sender=1, filename="ohne-datum.pdf", mime="application/pdf",
                          when=None)
    uuid = next_uuid()
    external[uuid] = PDF + b"H" * 220
    counter["file"] += 1
    file_pk = counter["file"]
    file_rows.append((file_pk, message, external_reference(uuid)))
    link(message, "ZDATA", file_pk)
    expected.append(
        ExpectedMedia("external", uuid, external[uuid], "Familie", "ohne-datum.pdf", None,
                      declared_mime="application/pdf")
    )

    # -- Referenz auf eine Datei, die im Backup fehlt -----------------------
    message = add_message(1, sender=1, filename="verschwunden.jpg", mime="image/jpeg",
                          when=base)
    missing_uuid = next_uuid()  # absichtlich NICHT in `external`
    counter["file"] += 1
    file_pk = counter["file"]
    file_rows.append((file_pk, message, external_reference(missing_uuid)))
    link(message, "ZDATA", file_pk)

    # -- Verwaiste Rueckrichtung nachbilden --------------------------------
    # Im echten Backup zeigt ZIMAGEDATA.ZMESSAGE auf nichts. Genau das hier.
    image_rows = [(pk, 99000 + pk, data, w, h) for pk, _, data, w, h in image_rows]
    video_rows = [(pk, 99000 + pk, data) for pk, _, data in video_rows]

    fixture.database = _write_store(
        contacts, conversations, messages, file_rows, image_rows, video_rows, audio_rows
    )
    fixture.external_blobs = external
    fixture.expected = expected
    return fixture


def _write_store(
    contacts: list[tuple[int, str, str | None, str | None, str | None]],
    conversations: list[tuple[int, int | None, str | None]],
    messages: list[dict[str, object]],
    file_rows: list[tuple[int, int | None, bytes]],
    image_rows: list[tuple[int, int | None, bytes, int, int]],
    video_rows: list[tuple[int, int | None, bytes]],
    audio_rows: list[tuple[int, int | None, bytes]],
) -> bytes:
    """Schreibt den Store als echte SQLite-Datei und gibt ihre Bytes zurueck."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / DATABASE_NAME
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.executescript(_SCHEMA)

            connection.execute(
                "INSERT INTO Z_METADATA VALUES (1, '00000000-0000-0000-0000-000000000000', NULL)"
            )
            for name, entity in _ENTITIES.items():
                connection.execute(
                    "INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, 0)",
                    (entity, name.removeprefix("Z").title()),
                )

            connection.executemany(
                "INSERT INTO ZCONTACT (Z_PK, Z_ENT, Z_OPT, ZIDENTITY, ZFIRSTNAME, "
                "ZLASTNAME, ZPUBLICNICKNAME) VALUES (?, ?, 1, ?, ?, ?, ?)",
                [(pk, _ENTITIES["ZCONTACT"], *rest) for pk, *rest in contacts],
            )
            connection.executemany(
                "INSERT INTO ZCONVERSATION (Z_PK, Z_ENT, Z_OPT, ZCONTACT, ZGROUPNAME) "
                "VALUES (?, ?, 1, ?, ?)",
                [(pk, _ENTITIES["ZCONVERSATION"], *rest) for pk, *rest in conversations],
            )
            connection.executemany(
                "INSERT INTO ZMESSAGE (Z_PK, Z_ENT, Z_OPT, ZCONVERSATION, ZSENDER, "
                "ZDATA, ZIMAGE, ZTHUMBNAIL, ZTHUMBNAIL1, ZTHUMBNAIL2, ZVIDEO, ZAUDIO, "
                "ZFILENAME, ZMIMETYPE, ZDATE) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        m["pk"], _ENTITIES["ZMESSAGE"], m["conversation"], m["sender"],
                        m.get("ZDATA"), m.get("ZIMAGE"), m.get("ZTHUMBNAIL"),
                        m.get("ZTHUMBNAIL1"), m.get("ZTHUMBNAIL2"), m.get("ZVIDEO"),
                        m.get("ZAUDIO"), m["filename"], m["mime"],
                        _timestamp(m["date"]),  # type: ignore[arg-type]
                    )
                    for m in messages
                ],
            )
            connection.executemany(
                "INSERT INTO ZFILEDATA (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, ZDATA) "
                "VALUES (?, ?, 1, ?, ?)",
                [(pk, _ENTITIES["ZFILEDATA"], msg, data) for pk, msg, data in file_rows],
            )
            connection.executemany(
                "INSERT INTO ZIMAGEDATA (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, ZDATA, ZWIDTH, "
                "ZHEIGHT) VALUES (?, ?, 1, ?, ?, ?, ?)",
                [
                    (pk, _ENTITIES["ZIMAGEDATA"], msg, data, w, h)
                    for pk, msg, data, w, h in image_rows
                ],
            )
            connection.executemany(
                "INSERT INTO ZVIDEODATA (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, ZDATA) "
                "VALUES (?, ?, 1, ?, ?)",
                [(pk, _ENTITIES["ZVIDEODATA"], msg, data) for pk, msg, data in video_rows],
            )
            connection.executemany(
                "INSERT INTO ZAUDIODATA (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, ZDATA) "
                "VALUES (?, ?, 1, ?, ?)",
                [(pk, _ENTITIES["ZAUDIODATA"], msg, data) for pk, msg, data in audio_rows],
            )
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()
