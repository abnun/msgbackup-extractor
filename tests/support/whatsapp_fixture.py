"""Erzeugt einen synthetischen WhatsApp-Core-Data-Store.

Nachgebildet wird die am echten Backup vermessene Struktur (siehe
`apps/whatsapp.py`). Entscheidend sind drei Eigenschaften, ohne die die Tests
nichts beweisen wuerden:

1. **Medien sind Dateien, keine Blobs.** Die Datenbank nennt Pfade.
2. **Dem Pfad in der Datenbank fehlt das Praefix `Message/`.** Genau daran
   entscheidet sich, ob die Zuordnung funktioniert - ein Fixture mit
   vollstaendigen Pfaden wuerde den Fehler verdecken.
3. **Beide Beziehungsrichtungen tragen.** Anders als bei Threema. Der
   Produktionscode muss trotzdem messen, und das laesst sich nur pruefen, wenn
   das Fixture beide Spalten fuellt.

Alle Namen und Nummern sind erfunden.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from tests.support.backup_builder import BackupFile

BUNDLE_ID: Final = "net.whatsapp.WhatsApp"
APP_DOMAIN: Final = f"AppDomain-{BUNDLE_ID}"
SHARED_DOMAIN: Final = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
PLUGIN_DOMAIN: Final = f"AppDomainPlugin-{BUNDLE_ID}.ShareExtension"

DATABASE_NAME: Final = "ChatStorage.sqlite"

#: Das Praefix, das die Datenbank auslaesst. Am echten Backup gemessen.
PATH_PREFIX: Final = "Message/"

APPLE_EPOCH: Final = datetime(2001, 1, 1, tzinfo=UTC)

JPEG: Final = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG: Final = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
MP4: Final = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
OPUS: Final = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead\x01\x02"
PDF: Final = b"%PDF-1.7\n%\xc3\xa4\xc3\xb6\n"

#: `ZSESSIONTYPE` = 1 heisst Gruppenchat.
GROUP_TYPE: Final = 1

_SCHEMA: Final = """
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB);
CREATE TABLE Z_PRIMARYKEY (
    Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);

CREATE TABLE ZWACHATSESSION (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZSESSIONTYPE INTEGER, ZLASTMESSAGE INTEGER,
    ZCONTACTJID VARCHAR, ZPARTNERNAME VARCHAR, ZLASTMESSAGEDATE TIMESTAMP);

CREATE TABLE ZWAMESSAGE (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZMESSAGETYPE INTEGER,
    ZFROMJID VARCHAR, ZTOJID VARCHAR, ZTEXT VARCHAR, ZMESSAGEDATE TIMESTAMP);

CREATE TABLE ZWAMEDIAITEM (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZMESSAGE INTEGER, ZFILESIZE INTEGER, ZMOVIEDURATION INTEGER,
    ZMEDIALOCALPATH VARCHAR, ZXMPPTHUMBPATH VARCHAR,
    ZTHUMBNAILLOCALPATH VARCHAR, ZTITLE VARCHAR, ZVCARDNAME VARCHAR);
"""

_ENTITIES: Final = {"ZWACHATSESSION": 1, "ZWAMESSAGE": 2, "ZWAMEDIAITEM": 3}


@dataclass(slots=True)
class ExpectedMedia:
    """Testerwartung fuer ein Medium."""

    relative_path: str
    content: bytes
    chat_name: str | None
    original_filename: str | None
    timestamp: datetime | None
    is_thumbnail: bool = False


@dataclass(slots=True)
class WhatsAppFixture:
    """Der erzeugte Store samt Dateien und Erwartungswerten."""

    database: bytes
    media: dict[str, bytes] = field(default_factory=dict)
    expected: list[ExpectedMedia] = field(default_factory=list)

    def backup_files(self) -> list[BackupFile]:
        written = datetime(2026, 8, 20, 5, 14, 0, tzinfo=UTC)
        files = [BackupFile(SHARED_DOMAIN, DATABASE_NAME, self.database)]
        files += [
            BackupFile(SHARED_DOMAIN, path, content)
            for path, content in sorted(self.media.items())
        ]
        # App-Interna, wie sie im echten Backup vorkommen.
        files += [
            BackupFile(APP_DOMAIN, "Library/Preferences/net.whatsapp.WhatsApp.plist",
                       b"bplist00" + b"\x00" * 40, last_modified=written),
            BackupFile(SHARED_DOMAIN, "BackedUpKeyValue.sqlite",
                       b"SQLite format 3\x00" + b"\x00" * 60, last_modified=written),
            BackupFile(PLUGIN_DOMAIN, "Library/Preferences/share.plist",
                       b"bplist00" + b"\x00" * 8, last_modified=written),
        ]
        return files

    def by_path(self, relative_path: str) -> ExpectedMedia:
        for candidate in self.expected:
            if candidate.relative_path == relative_path:
                return candidate
        raise KeyError(relative_path)


def _stamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - APPLE_EPOCH).total_seconds()


def build_whatsapp_store() -> WhatsAppFixture:
    """Baut einen Store, der die relevanten Faelle abdeckt.

    * Gruppenchat mit Bild samt Vorschaubild
    * Einzelchat mit Video, Sprachnachricht (Opus) und Dokument mit `ZTITLE`
    * Medieneintrag, dessen Datei im Backup fehlt
    * Datei unter `Message/Media/`, die die Datenbank nicht kennt (Waise)
    * Nachricht ohne Zeitstempel
    """
    base = datetime(2024, 6, 15, 12, 30, 0, tzinfo=UTC)
    media: dict[str, bytes] = {}
    expected: list[ExpectedMedia] = []

    # (Z_PK, ZSESSIONTYPE, ZPARTNERNAME)
    sessions = [
        (1, GROUP_TYPE, "Wanderfreunde"),
        (2, 0, "Erika Beispiel"),
        (3, 0, None),  # ohne Namen -> Fallback auf die Chat-ID
    ]
    messages: list[tuple] = []
    items: list[tuple] = []

    def add(
        *,
        session: int | None,
        db_path: str | None,
        content: bytes | None,
        thumb_path: str | None = None,
        thumb_content: bytes | None = None,
        title: str | None = None,
        when: datetime | None = base,
        chat_name: str | None = None,
    ) -> None:
        message_pk = len(messages) + 1
        item_pk = len(items) + 1
        messages.append((message_pk, session, item_pk, _stamp(when)))
        items.append((item_pk, message_pk, db_path, thumb_path, title,
                      len(content) if content else None))
        if db_path and content is not None:
            full = PATH_PREFIX + db_path
            media[full] = content
            expected.append(ExpectedMedia(full, content, chat_name, title, when))
        if thumb_path and thumb_content is not None:
            full = PATH_PREFIX + thumb_path
            media[full] = thumb_content
            expected.append(
                ExpectedMedia(full, thumb_content, chat_name, title, when,
                              is_thumbnail=True)
            )

    add(session=1, db_path="Media/gruppe-a/1/0/foto.jpg", content=JPEG + b"A" * 400,
        thumb_path="Media/gruppe-a/1/0/foto.thumb", thumb_content=JPEG + b"t" * 90,
        chat_name="Wanderfreunde")
    add(session=2, db_path="Media/kontakt-b/2/1/clip.mp4", content=MP4 + b"B" * 900,
        chat_name="Erika Beispiel")
    add(session=2, db_path="Media/kontakt-b/2/2/sprachnachricht.opus",
        content=OPUS + b"C" * 200, chat_name="Erika Beispiel")
    add(session=2, db_path="Media/kontakt-b/2/3/anhang.bin", content=PDF + b"D" * 300,
        title="Rechnung 2024.pdf", chat_name="Erika Beispiel")
    add(session=3, db_path="Media/kontakt-c/3/0/bild.png", content=PNG + b"E" * 250,
        chat_name="chat-3")
    # Ohne Zeitstempel
    add(session=1, db_path="Media/gruppe-a/1/1/ohne-datum.jpg", content=JPEG + b"F" * 150,
        when=None, chat_name="Wanderfreunde")
    # Datei fehlt im Backup: Eintrag ja, Inhalt nein
    add(session=1, db_path="Media/gruppe-a/1/2/verschwunden.jpg", content=None,
        chat_name="Wanderfreunde")
    # Titel, der kein Dateiname ist - darf nicht als solcher gelten
    add(session=2, db_path="Media/kontakt-b/2/4/ort.jpg", content=JPEG + b"G" * 120,
        title="Ein Ort ohne Endung", chat_name="Erika Beispiel")

    # Waise: Datei da, aber die Datenbank kennt sie nicht.
    media[PATH_PREFIX + "Media/gruppe-a/1/3/waise.jpg"] = JPEG + b"H" * 110

    database = _write_store(sessions, messages, items)
    return WhatsAppFixture(database=database, media=media, expected=expected)


def _write_store(sessions: list[tuple], messages: list[tuple], items: list[tuple]) -> bytes:
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
                    "INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, 0)", (entity, name)
                )
            connection.executemany(
                "INSERT INTO ZWACHATSESSION (Z_PK, Z_ENT, Z_OPT, ZSESSIONTYPE, "
                "ZPARTNERNAME, ZCONTACTJID) VALUES (?, ?, 1, ?, ?, ?)",
                [
                    (pk, _ENTITIES["ZWACHATSESSION"], kind, name, f"{pk}@s.example")
                    for pk, kind, name in sessions
                ],
            )
            connection.executemany(
                "INSERT INTO ZWAMESSAGE (Z_PK, Z_ENT, Z_OPT, ZCHATSESSION, ZMEDIAITEM, "
                "ZMESSAGEDATE) VALUES (?, ?, 1, ?, ?, ?)",
                [
                    (pk, _ENTITIES["ZWAMESSAGE"], session, item, stamp)
                    for pk, session, item, stamp in messages
                ],
            )
            connection.executemany(
                "INSERT INTO ZWAMEDIAITEM (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, "
                "ZMEDIALOCALPATH, ZXMPPTHUMBPATH, ZTITLE, ZFILESIZE) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                [
                    (pk, _ENTITIES["ZWAMEDIAITEM"], message, db_path, thumb, title, size)
                    for pk, message, db_path, thumb, title, size in items
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()
