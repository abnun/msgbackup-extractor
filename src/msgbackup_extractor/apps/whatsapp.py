"""Profil fuer WhatsApp (iOS).

WhatsApp speichert seine Medien grundlegend anders als Threema. Am echten
Backup vermessen:

* Die Nachrichtendatenbank ist `ChatStorage.sqlite` in
  `AppDomainGroup-group.net.whatsapp.WhatsApp.shared`, ein Core-Data-Store mit
  Nachrichten, Medieneintraegen und Chats in eigenen Entitaeten.
* Medien liegen als **echte Dateien** unter `Message/Media/…`, nicht als Blobs
  in der Datenbank. Die Datenbank nennt in `ZWAMEDIAITEM.ZMEDIALOCALPATH` einen
  Pfad, dem das Praefix `Message/` fehlt: mit Praefix loesen nahezu alle
  Werten auf eine Backupdatei auf, ohne Praefix keiner.
* Vorschaubilder stehen in `ZXMPPTHUMBPATH`, ebenfalls als Datei
  (ebenfalls nahezu alle aufloesbar). `ZTHUMBNAILLOCALPATH` ist nie gefuellt.
* Beide Beziehungsrichtungen tragen: `ZWAMEDIAITEM.ZMESSAGE` und
  `ZWAMESSAGE.ZMEDIAITEM` in jedem einzelnen Fall. Anders als bei Threema, wo eine
  Seite vollstaendig verwaist ist - weshalb die Richtung trotzdem gemessen und
  nicht angenommen wird.
* Chatnamen stehen in `ZWACHATSESSION.ZPARTNERNAME` (alle 569 gefuellt);
  `ZSESSIONTYPE = 1` kennzeichnet Gruppen.
* Zeitstempel zaehlen ab 2001 (Core Data), [Anzahl entfernt] plausibel.

**Datenschutzhinweis:** Die Medienpfade enthalten Telefonnummern und Gruppen-IDs
als Verzeichnisnamen. Sie gehen deshalb nicht in Logausgaben und nicht in das
Export-Manifest; dort steht die `fileID`, nicht der Pfad.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final

from msgbackup_extractor.apps.base import (
    AppProfile,
    DatabaseCandidate,
    DatabaseRole,
    MediaContext,
    MediaEnumeration,
)
from msgbackup_extractor.apps.core_data import (
    Direction,
    SchemaView,
    apple_datetime,
    is_core_data,
    measure_direction,
    quote,
)
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.models import (
    ChatReference,
    ManifestEntry,
    MediaItem,
    MediaSource,
    SourceKind,
    TimestampSource,
)

logger = get_logger("whatsapp")

MESSAGE_TABLE: Final = "ZWAMESSAGE"
MEDIA_TABLE: Final = "ZWAMEDIAITEM"
SESSION_TABLE: Final = "ZWACHATSESSION"

#: Spalte in ZWAMEDIAITEM mit dem Pfad des Originals.
MEDIA_PATH_COLUMN: Final = "ZMEDIALOCALPATH"

#: Spalten, die ein Vorschaubild benennen koennen - in dieser Reihenfolge.
THUMBNAIL_PATH_COLUMNS: Final = ("ZXMPPTHUMBPATH", "ZTHUMBNAILLOCALPATH")

#: Kandidaten fuer das Praefix, das den DB-Pfad zum Backup-Pfad macht.
#: Welches gilt, wird gemessen - WhatsApp koennte seine Ablage umstellen.
PATH_PREFIX_CANDIDATES: Final = ("Message/", "", "Message/Media/", "Media/")

#: `ZSESSIONTYPE` = 1 kennzeichnet einen Gruppenchat.
GROUP_SESSION_TYPE: Final = 1

#: So viele Werte genuegen, um das Praefix zu bestimmen.
_PREFIX_SAMPLE: Final = 400


@dataclass(frozen=True, slots=True)
class _Prefix:
    """Das gemessene Praefix samt Trefferquote."""

    value: str
    matched: int
    total: int

    @property
    def is_reliable(self) -> bool:
        """Traegt das Praefix? Unter der Haelfte gilt es als nicht belegt."""
        return self.total > 0 and self.matched * 2 >= self.total

    @property
    def evidence(self) -> str:
        label = self.value or "(kein Praefix)"
        return f"{label}: {self.matched} von {self.total} aufgeloest"


class WhatsAppProfile(AppProfile):
    """Erkennung von WhatsApp und Zuordnung seiner Medien."""

    name = "WhatsApp"
    slug = "whatsapp"

    #: Namensraum statt fester Bezeichner: erfasst auch WhatsApp Business,
    #: ohne dessen exakten Bundle Identifier zu behaupten.
    bundle_namespaces = ("net.whatsapp.",)

    #: Nur ein Diagnosehinweis; erkannt gilt erst, was im Backup vorkommt.
    known_bundle_ids = ("net.whatsapp.WhatsApp",)

    group_namespaces = ("group.net.whatsapp",)

    def requires_tables(self) -> tuple[str, ...]:
        return (MESSAGE_TABLE, SESSION_TABLE, MEDIA_TABLE)

    def enumerate_media(self, context: MediaContext) -> MediaEnumeration:
        reason = self.supports_schema(context.schemas)
        if reason is not None:
            return MediaEnumeration(unsupported_reason=reason)
        return _WhatsAppReader(context).enumerate()

    def classify_databases(
        self, candidates: tuple[DatabaseCandidate, ...]
    ) -> tuple[DatabaseRole, ...]:
        """Ordnet Datenbanken eine Rolle zu - anhand nachweisbarer Struktur."""
        roles: list[DatabaseRole] = []
        for candidate in candidates:
            tables = {table.upper() for table in candidate.tables}
            if not tables:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="unknown",
                        reason=(
                            "Das Schema wurde nicht eingelesen. Ohne Tabellenliste "
                            "ist keine Aussage moeglich."
                        ),
                    )
                )
                continue

            required = {name.upper() for name in self.requires_tables()}
            if is_core_data(tables) and required <= tables:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="messages",
                        reason=(
                            "Core-Data-Store mit "
                            f"{', '.join(sorted(required))} - die "
                            "Nachrichtendatenbank von WhatsApp."
                        ),
                        confidence="high",
                    )
                )
            elif is_core_data(tables):
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="metadata",
                        reason=(
                            "Core-Data-Store, aber ohne die Nachrichtentabellen. "
                            "WhatsApp legt mehrere Nebendatenbanken an."
                        ),
                        confidence="medium",
                    )
                )
            else:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="unknown",
                        reason=(
                            "Kein Core-Data-Store. Tabellen: "
                            f"{', '.join(sorted(candidate.tables)[:8])}."
                        ),
                        confidence="low",
                    )
                )
        return tuple(roles)


class _WhatsAppReader:
    """Liest die Medienzuordnung aus `ChatStorage.sqlite`."""

    def __init__(self, context: MediaContext) -> None:
        self.context = context
        self.connection = context.connection
        self.schema = SchemaView(context.schemas)
        self.notes: list[str] = []
        self.unresolved = 0

    # -- Praefix der Medienpfade -------------------------------------------

    def detect_prefix(self, column: str) -> _Prefix:
        """Bestimmt, welches Praefix die DB-Pfade zu Backup-Pfaden macht.

        Gemessen statt angenommen: WhatsApp koennte seine Ablage umstellen, und
        ein fest verdrahtetes Praefix wuerde dann stillschweigend nichts mehr
        finden. Eine Stichprobe genuegt, weil das Praefix fuer alle Werte
        dasselbe ist.
        """
        rows = self.connection.execute(
            f"SELECT {quote(column)} FROM {quote(MEDIA_TABLE)} "
            f"WHERE {quote(column)} IS NOT NULL AND {quote(column)} != '' "
            f"LIMIT {_PREFIX_SAMPLE}"
        ).fetchall()
        values = [row[0] for row in rows if isinstance(row[0], str)]
        if not values:
            return _Prefix(value="", matched=0, total=0)

        known = self.context.entries_by_path
        best = _Prefix(value="", matched=0, total=len(values))
        for candidate in PATH_PREFIX_CANDIDATES:
            matched = sum(1 for value in values if candidate + value in known)
            if matched > best.matched:
                best = _Prefix(value=candidate, matched=matched, total=len(values))
            if matched == len(values):
                break
        return best

    # -- Chats --------------------------------------------------------------

    def chat_map(self) -> dict[int, ChatReference]:
        """Konversations-Primaerschluessel -> Chat.

        Der Name kommt aus `ZPARTNERNAME`. Fehlt er, bleibt er None und der
        Export verwendet die Chat-ID - die Kontakt-JID waere ein Ersatz, aber
        sie ist eine Telefonnummer und hat in einem Verzeichnisnamen nichts zu
        suchen.
        """
        if not self.schema.has(SESSION_TABLE, "Z_PK"):
            return {}
        columns = ["Z_PK", *self.schema.present(SESSION_TABLE, ("ZPARTNERNAME", "ZSESSIONTYPE"))]
        chats: dict[int, ChatReference] = {}
        for row in self.connection.execute(
            f"SELECT {', '.join(quote(c) for c in columns)} "
            f"FROM {quote(SESSION_TABLE)}"
        ):
            values = dict(zip(columns, row, strict=True))
            pk = values["Z_PK"]
            name = (values.get("ZPARTNERNAME") or "").strip() or None
            is_group = values.get("ZSESSIONTYPE") == GROUP_SESSION_TYPE
            chats[pk] = ChatReference(
                chat_id=str(pk),
                name=name,
                kind="group" if is_group else "direct",
            )
        return chats

    # -- Nachrichten --------------------------------------------------------

    def message_map(self) -> dict[int, dict[str, object]]:
        """Nachrichten-Primaerschluessel -> Chat und Zeitpunkt.

        `ZTEXT` wird bewusst **nicht** gelesen: Nachrichtentexte werden nicht
        exportiert und haben in keinem Zwischenschritt etwas zu suchen.
        """
        columns = ["Z_PK", *self.schema.present(MESSAGE_TABLE, ("ZCHATSESSION", "ZMESSAGEDATE"))]
        result: dict[int, dict[str, object]] = {}
        for row in self.connection.execute(
            f"SELECT {', '.join(quote(c) for c in columns)} "
            f"FROM {quote(MESSAGE_TABLE)}"
        ):
            values = dict(zip(columns, row, strict=True))
            result[values["Z_PK"]] = {
                "session": values.get("ZCHATSESSION"),
                "date": apple_datetime(values.get("ZMESSAGEDATE")),
            }
        return result

    # -- Beziehungsrichtung -------------------------------------------------

    def media_to_message(self) -> tuple[dict[int, int], Direction | None]:
        """Medien-Primaerschluessel -> Nachrichten-Primaerschluessel.

        Beide Richtungen werden vermessen und die tragende verwendet. Am echten
        Backup tragen beide; bei Threema ist eine vollstaendig verwaist, deshalb
        wird nicht auf eine davon gesetzt.
        """
        candidates: list[Direction] = []
        if self.schema.has(MEDIA_TABLE, MESSAGE_TABLE) and self.schema.has(MESSAGE_TABLE, "Z_PK"):
            candidates.append(
                measure_direction(self.connection, MEDIA_TABLE, MESSAGE_TABLE, MESSAGE_TABLE)
            )
        if self.schema.has(MESSAGE_TABLE, "ZMEDIAITEM") and self.schema.has(MEDIA_TABLE, "Z_PK"):
            candidates.append(
                measure_direction(self.connection, MESSAGE_TABLE, "ZMEDIAITEM", MEDIA_TABLE)
            )

        carrying = [d for d in candidates if d.carries]
        for direction in candidates:
            if not direction.carries and direction.total:
                self.notes.append(
                    f"{direction.evidence} traegt nicht ({direction.total} Werte, "
                    "kein Treffer); die Gegenrichtung wird verwendet."
                )
        if not carrying:
            return {}, None

        best = max(carrying, key=lambda d: d.matched)
        mapping: dict[int, int] = {}
        if best.from_table == MEDIA_TABLE:
            rows = self.connection.execute(
                f"SELECT a.Z_PK, a.{quote(MESSAGE_TABLE)} FROM {quote(MEDIA_TABLE)} a "
                f"JOIN {quote(MESSAGE_TABLE)} b ON b.Z_PK = a.{quote(MESSAGE_TABLE)}"
            )
        else:
            rows = self.connection.execute(
                f"SELECT a.ZMEDIAITEM, a.Z_PK FROM {quote(MESSAGE_TABLE)} a "
                f"JOIN {quote(MEDIA_TABLE)} b ON b.Z_PK = a.ZMEDIAITEM"
            )
        for media_pk, message_pk in rows:
            mapping.setdefault(media_pk, message_pk)
        return mapping, best

    # -- Enumeration --------------------------------------------------------

    def enumerate(self) -> MediaEnumeration:
        media_prefix = self.detect_prefix(MEDIA_PATH_COLUMN)
        if not media_prefix.is_reliable:
            return MediaEnumeration(
                notes=tuple(self.notes),
                unsupported_reason=(
                    "Die Medienpfade aus der WhatsApp-Datenbank liessen sich "
                    "keiner Datei im Backup zuordnen "
                    f"({media_prefix.evidence}). Eine Zuordnung waere geraten."
                ),
            )
        self.notes.append(f"Praefix der Medienpfade bestimmt - {media_prefix.evidence}.")

        thumbnail_column = next(
            (c for c in THUMBNAIL_PATH_COLUMNS if self.schema.has(MEDIA_TABLE, c)), None
        )
        thumbnail_prefix = (
            self.detect_prefix(thumbnail_column) if thumbnail_column else _Prefix("", 0, 0)
        )
        if thumbnail_column and not thumbnail_prefix.is_reliable:
            self.notes.append(
                f"Vorschaubilder ueber {thumbnail_column} liessen sich nicht "
                f"zuordnen ({thumbnail_prefix.evidence}); sie werden uebergangen."
            )
            thumbnail_column = None

        chats = self.chat_map()
        messages = self.message_map()
        owners, direction = self.media_to_message()
        if direction is not None:
            self.notes.append(f"Beziehung gemessen - {direction!r}.")

        columns = ["Z_PK", MEDIA_PATH_COLUMN]
        if thumbnail_column:
            columns.append(thumbnail_column)
        columns += list(self.schema.present(MEDIA_TABLE, ("ZTITLE", "ZFILESIZE")))

        items: list[MediaItem] = []
        thumbnails: list[tuple[int, str | None]] = []

        for row in self.connection.execute(
            f"SELECT {', '.join(quote(c) for c in columns)} "
            f"FROM {quote(MEDIA_TABLE)}"
        ):
            values = dict(zip(columns, row, strict=True))
            media_pk = values["Z_PK"]
            message_pk = owners.get(media_pk)
            message = messages.get(message_pk) if message_pk is not None else None
            chat = chats.get(message["session"]) if message and message.get("session") else None
            timestamp = message.get("date") if message else None
            title = values.get("ZTITLE")

            original = self._entry(values.get(MEDIA_PATH_COLUMN), media_prefix.value)
            original_identity: str | None = None
            if original is not None:
                item = self._item(
                    entry=original,
                    chat=chat,
                    timestamp=timestamp,
                    title=title,
                    message_pk=message_pk,
                    evidence=direction.evidence if direction else None,
                    is_thumbnail=False,
                )
                original_identity = item.source.identity()
                items.append(item)

            if not thumbnail_column:
                continue
            thumbnail = self._entry(values.get(thumbnail_column), thumbnail_prefix.value)
            if thumbnail is None:
                continue
            items.append(
                self._item(
                    entry=thumbnail,
                    chat=chat,
                    timestamp=timestamp,
                    title=title,
                    message_pk=message_pk,
                    evidence=direction.evidence if direction else None,
                    is_thumbnail=True,
                )
            )
            thumbnails.append((len(items) - 1, original_identity))

        for index, original_identity in thumbnails:
            if original_identity is not None:
                items[index] = replace(items[index], thumbnail_of=original_identity)

        if self.unresolved:
            self.notes.append(
                f"{self.unresolved} Pfadangaben zeigen auf eine Datei, die im "
                "Backup nicht vorhanden ist. Diese Medien sind nicht extrahierbar."
            )

        logger.debug("%d WhatsApp-Medien aufgezaehlt", len(items))
        return MediaEnumeration(
            items=tuple(items),
            dangling_references=self.unresolved,
            notes=tuple(self.notes),
        )

    # -- Hilfen -------------------------------------------------------------

    def _entry(self, value: object, prefix: str) -> ManifestEntry | None:
        """Loest eine Pfadangabe auf einen Manifest-Eintrag auf."""
        if not isinstance(value, str) or not value:
            return None
        entry = self.context.entries_by_path.get(prefix + value)
        if entry is None:
            self.unresolved += 1
            return None
        return entry

    def _item(
        self,
        *,
        entry: ManifestEntry,
        chat: ChatReference | None,
        timestamp: datetime | None,
        title: object,
        message_pk: int | None,
        evidence: str | None,
        is_thumbnail: bool,
    ) -> MediaItem:
        return MediaItem(
            source=MediaSource(
                kind=SourceKind.EXTERNAL_FILE,
                file_id=entry.file_id,
                domain=entry.domain,
                relative_path=entry.relative_path,
            ),
            size=entry.size,
            chat=chat,
            original_filename=self._filename(title),
            timestamp=timestamp,
            timestamp_source=TimestampSource.MESSAGE if timestamp else None,
            is_thumbnail=is_thumbnail,
            message_id=str(message_pk) if message_pk is not None else None,
            evidence=evidence,
        )

    @staticmethod
    def _filename(title: object) -> str | None:
        """`ZTITLE` traegt bei Dokumenten den Originalnamen, sonst Beschreibungen.

        Als Dateiname gilt nur, was eine Endung hat. Alles andere waere ein
        erfundener Name - bei Fotos vergibt WhatsApp gar keinen.
        """
        if not isinstance(title, str):
            return None
        candidate = title.strip()
        if not candidate or "/" in candidate:
            return None
        suffix = PurePosixPath(candidate).suffix
        if not (1 < len(suffix) <= 6):
            return None
        return candidate
