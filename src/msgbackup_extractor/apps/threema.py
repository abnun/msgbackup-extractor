"""Profil fuer Threema (iOS).

Bewusste Zurueckhaltung: Dieses Profil erkennt Threema am Bundle Identifier und
klassifiziert gefundene Datenbanken anhand **strukturell nachweisbarer**
Merkmale. Es setzt keine Dateipfade, keine Tabellennamen und keine Spalten
voraus.

Threema iOS verwendet Core Data. Das ist keine Vermutung, sondern an jedem
Core-Data-Store nachweisbar: er enthaelt immer die Verwaltungstabellen
`Z_METADATA` und `Z_PRIMARYKEY`, und Entitaetstabellen tragen das Praefix `Z`.
Genau diese Merkmale werden geprueft. Welche Entitaet welchen Zweck hat, wird
aus dem Namen abgeleitet - und wenn das nicht eindeutig ist, lautet die Rolle
`unknown` mit Begruendung, statt geraten zu werden.

Die eigentliche Chat-Zuordnung (`link_media`) bleibt bis Phase 5 unimplementiert
und liefert bis dahin nichts. Sie wird auf Basis des real vorgefundenen Schemas
gebaut, nicht auf Basis von Annahmen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from msgbackup_extractor.apps.base import (
    AppProfile,
    DatabaseCandidate,
    DatabaseRole,
    MediaContext,
    MediaEnumeration,
)
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.models import (
    ChatReference,
    MediaItem,
    MediaSource,
    SourceKind,
)

logger = get_logger("threema")

#: Verwaltungstabellen, die jeden Core-Data-Store ausweisen.
CORE_DATA_MARKERS: Final = ("Z_METADATA", "Z_PRIMARYKEY")

#: Namensbestandteile, die auf eine Rolle hindeuten. Reihenfolge = Priorität.
_ROLE_HINTS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("messages", ("message", "nachricht", "chatmessage")),
    ("conversations", ("conversation", "chat", "group", "gruppe")),
    ("contacts", ("contact", "kontakt", "identity", "sender")),
    ("media", ("blob", "media", "image", "video", "audio", "file", "thumbnail", "attachment")),
    ("metadata", ("metadata", "setting", "preference", "tag", "ballot")),
)


class ThreemaProfile(AppProfile):
    """Erkennung von Threema und seinen Varianten."""

    name = "Threema"
    slug = "threema"

    #: Namensraum statt fester Bezeichner: erfasst Threema, Threema Work und
    #: Threema OnPrem, ohne deren exakte Bundle Identifier zu behaupten.
    bundle_namespaces = ("ch.threema.",)

    #: Nur Diagnosehinweise. Ein Eintrag hier gilt erst als erkannt, wenn er
    #: im Backup tatsaechlich vorkommt.
    known_bundle_ids = ("ch.threema.iapp",)

    #: App-Group-Container tragen den Bundle Identifier nicht immer vollstaendig.
    group_namespaces = ("group.ch.threema",)

    def requires_tables(self) -> tuple[str, ...]:
        return (MESSAGE_TABLE, CONVERSATION_TABLE)

    def enumerate_media(self, context: MediaContext) -> MediaEnumeration:
        """Zaehlt Threema-Medien auf und ordnet sie Chats zu.

        Die Richtung jeder Beziehung wird zur Laufzeit gemessen (siehe
        `_ThreemaReader.carrying_links`), weil Core Data sie je Entitaet auf
        einer anderen Seite ablegt. Traegt keine Richtung, wird nichts
        zugeordnet und der Grund gemeldet.
        """
        reason = self.supports_schema(context.schemas)
        if reason is not None:
            return MediaEnumeration(unsupported_reason=reason)
        return _ThreemaReader(context).enumerate()

    def classify_databases(
        self, candidates: tuple[DatabaseCandidate, ...]
    ) -> tuple[DatabaseRole, ...]:
        """Ordnet Datenbanken anhand nachweisbarer Struktur eine Rolle zu."""
        roles: list[DatabaseRole] = []

        for candidate in candidates:
            tables = candidate.tables
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

            upper = {table.upper() for table in tables}
            is_core_data = all(marker in upper for marker in CORE_DATA_MARKERS)

            entities = tuple(
                table
                for table in tables
                if table.upper().startswith("Z") and table.upper() not in upper.intersection(
                    {"Z_METADATA", "Z_PRIMARYKEY", "Z_MODELCACHE"}
                )
            )

            found_roles = self._roles_from_names(entities or tables)

            if is_core_data and found_roles:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="messages" if "messages" in found_roles else next(iter(found_roles)),
                        reason=(
                            "Core-Data-Store (Z_METADATA und Z_PRIMARYKEY vorhanden) "
                            f"mit Entitaeten fuer: {', '.join(sorted(found_roles))}."
                        ),
                        confidence="high",
                    )
                )
            elif is_core_data:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="unknown",
                        reason=(
                            "Core-Data-Store, aber keine der Entitaeten laesst sich "
                            f"einer Rolle zuordnen. Entitaeten: "
                            f"{', '.join(sorted(entities)[:10]) or '(keine)'}."
                        ),
                        confidence="low",
                    )
                )
            elif found_roles:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role=next(iter(found_roles)),
                        reason=(
                            "Kein Core-Data-Store, aber Tabellennamen deuten auf "
                            f"{', '.join(sorted(found_roles))} hin."
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
                            "Weder Core-Data-Merkmale noch zuordenbare Tabellennamen. "
                            f"Tabellen: {', '.join(sorted(tables)[:10])}."
                        ),
                        confidence="low",
                    )
                )

        return tuple(roles)

    @staticmethod
    def _roles_from_names(tables: tuple[str, ...]) -> set[str]:
        """Welche Rollen die Tabellennamen nahelegen."""
        lowered = [table.lower() for table in tables]
        found: set[str] = set()
        for role, hints in _ROLE_HINTS:
            if any(hint in name for name in lowered for hint in hints):
                found.add(role)
        return found


# ---------------------------------------------------------------------------
# Medien-Enumeration
# ---------------------------------------------------------------------------

#: Praefix und Suffix einer Referenz auf `_EXTERNAL_DATA`.
#: Am echten Backup vermessen: genau 38 Byte, 0x02 + 36 Byte UUID + 0x00.
EXTERNAL_REF_LENGTH: Final = 38
EXTERNAL_REF_PREFIX: Final = 0x02
EXTERNAL_REF_SUFFIX: Final = 0x00

#: Core Data stellt jedem Blob ein Markierungsbyte voran: `0x01` heisst
#: "die Daten folgen direkt", `0x02` heisst "es folgt eine Referenz auf
#: _EXTERNAL_DATA". Am echten Backup nachgezaehlt: alle [Anzahl entfernt] Inline-Blobs
#: beginnen mit 0x01, alle [Anzahl entfernt] Referenzen mit 0x02.
#:
#: Dieses Byte MUSS abgeschnitten werden. Ohne das waere jede aus der
#: Datenbank exportierte Datei um ein Byte verschoben - ein JPEG liesse sich
#: nicht oeffnen, und die Signaturerkennung wuerde nichts erkennen.
INLINE_PREFIX: Final = 0x01
INLINE_PREFIX_LENGTH: Final = 1

#: Verzeichnis der externen Core-Data-Blobs, relativ zur Datenbank.
EXTERNAL_DATA_DIR: Final = ".ThreemaData_SUPPORT/_EXTERNAL_DATA"

MESSAGE_TABLE: Final = "ZMESSAGE"
CONVERSATION_TABLE: Final = "ZCONVERSATION"
CONTACT_TABLE: Final = "ZCONTACT"

#: Beziehungen zwischen Nachricht und Datenblob.
#: (Spalte in ZMESSAGE, Datentabelle, ist_Vorschaubild)
_MEDIA_LINKS: Final[tuple[tuple[str, str, bool], ...]] = (
    ("ZDATA", "ZFILEDATA", False),
    ("ZIMAGE", "ZIMAGEDATA", False),
    ("ZVIDEO", "ZVIDEODATA", False),
    ("ZAUDIO", "ZAUDIODATA", False),
    ("ZTHUMBNAIL", "ZIMAGEDATA", True),
    ("ZTHUMBNAIL1", "ZIMAGEDATA", True),
    ("ZTHUMBNAIL2", "ZIMAGEDATA", True),
)

#: Alle Tabellen, die Blobs enthalten koennen.
_DATA_TABLES: Final = ("ZFILEDATA", "ZIMAGEDATA", "ZVIDEODATA", "ZAUDIODATA")

BLOB_COLUMN: Final = "ZDATA"

#: Sekunden zwischen Unix- und Apple-Epoche.
_APPLE_EPOCH: Final = datetime(2001, 1, 1, tzinfo=UTC)


def is_external_reference(blob: bytes | memoryview | None) -> bool:
    """Ist dieser Blob eine Referenz auf eine Datei in `_EXTERNAL_DATA`?

    Kriterium aus der Vermessung des echten Backups: genau 38 Byte, erstes Byte
    `0x02`, letztes `0x00`, dazwischen 36 Byte druckbares ASCII. Die Pruefung
    ist absichtlich streng - ein zufaellig 38 Byte langer Inhalt soll nicht als
    Referenz durchgehen.
    """
    if blob is None:
        return False
    raw = bytes(blob)
    if len(raw) != EXTERNAL_REF_LENGTH:
        return False
    if raw[0] != EXTERNAL_REF_PREFIX or raw[-1] != EXTERNAL_REF_SUFFIX:
        return False
    return all(0x2D <= byte <= 0x7A for byte in raw[1:-1])


def external_reference_name(blob: bytes | memoryview) -> str:
    """Der Dateiname, auf den eine Referenz zeigt."""
    return bytes(blob)[1:-1].decode("ascii")


def _apple_datetime(value: object, *, now: datetime | None = None) -> datetime | None:
    """Core-Data-Zeitstempel (Sekunden seit 2001) in ein `datetime`.

    Anders als MBFile zaehlt Core Data ab 2001 - das ist am echten Backup
    belegt: die so umgerechneten Nachrichtendaten liegen zwischen 2017 und
    heute, waehrend dieselben Werte als Unix-Zeit in den Neunzigern landen
    wuerden.

    Die Obergrenze ist "jetzt" mit einem Tag Spielraum: eine Nachricht kann
    nicht in der Zukunft gesendet worden sein. Unplausible Werte werden zu
    None, weil eine erfundene Zeit schlimmer ist als keine.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        stamp = _APPLE_EPOCH + timedelta(seconds=float(value))
    except (OverflowError, ValueError, OSError):
        return None
    upper = (now or datetime.now(UTC)) + timedelta(days=1)
    if not (datetime(2007, 1, 1, tzinfo=UTC) <= stamp <= upper):
        logger.debug("Unplausibler Core-Data-Zeitstempel verworfen: %s", stamp.isoformat())
        return None
    return stamp


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class _Link:
    """Eine als tragend nachgewiesene Beziehung."""

    message_column: str
    data_table: str
    is_thumbnail: bool
    #: "message" = ZMESSAGE.<Spalte> -> <Tabelle>.Z_PK
    #: "data"    = <Tabelle>.ZMESSAGE -> ZMESSAGE.Z_PK
    direction: str
    matched: int
    total: int

    @property
    def evidence(self) -> str:
        if self.direction == "message":
            return f"{MESSAGE_TABLE}.{self.message_column} -> {self.data_table}.Z_PK"
        return f"{self.data_table}.{MESSAGE_TABLE} -> {MESSAGE_TABLE}.Z_PK"


class _ThreemaReader:
    """Liest die Zuordnung aus einem Threema-Core-Data-Store.

    Bewusst als eigene Klasse: die Enumeration braucht mehrere Zwischenkarten,
    und als Methodenkette bleibt jeder Schritt einzeln pruefbar.
    """

    def __init__(self, context: MediaContext) -> None:
        self.context = context
        self.connection = context.connection
        self.columns: dict[str, set[str]] = {
            name.upper(): {c.upper() for c in schema.columns}
            for name, schema in context.schemas.items()
        }
        self.notes: list[str] = []
        self.dangling = 0
        #: Inline-Blobs ohne das erwartete Markierungsbyte 0x01.
        self.unexpected_prefixes = 0

    # -- Schema-Hilfen ------------------------------------------------------

    def has(self, table: str, *columns: str) -> bool:
        available = self.columns.get(table.upper())
        if available is None:
            return False
        return all(column.upper() in available for column in columns)

    # -- Richtung der Beziehungen ------------------------------------------

    def carrying_links(self) -> tuple[_Link, ...]:
        """Ermittelt fuer jede Beziehung die tatsaechlich tragende Richtung.

        Core Data legt den Fremdschluessel je Beziehung nur auf einer Seite ab,
        und welche das ist, unterscheidet sich zwischen den Entitaeten. Am
        echten Backup ist `ZIMAGEDATA.ZMESSAGE` zu 100 % verwaist, waehrend
        `ZFILEDATA.ZMESSAGE` traegt. Deshalb wird gemessen statt angenommen.
        """
        links: list[_Link] = []

        for column, table, is_thumbnail in _MEDIA_LINKS:
            if not self.has(MESSAGE_TABLE, column) or not self.has(table, "Z_PK"):
                continue

            matched, total = self.connection.execute(
                f"SELECT COUNT(d.Z_PK), COUNT(m.{_quote(column)}) "
                f"FROM {_quote(MESSAGE_TABLE)} m "
                f"LEFT JOIN {_quote(table)} d ON d.Z_PK = m.{_quote(column)} "
                f"WHERE m.{_quote(column)} IS NOT NULL"
            ).fetchone()

            if total and matched == total:
                links.append(
                    _Link(column, table, is_thumbnail, "message", matched, total)
                )
            elif total and matched:
                # Teilweise tragend: verwenden, aber im Bericht erwaehnen.
                links.append(
                    _Link(column, table, is_thumbnail, "message", matched, total)
                )
                self.notes.append(
                    f"{MESSAGE_TABLE}.{column} verweist {total - matched} von {total} "
                    f"Mal auf keinen Eintrag in {table}."
                )
            elif total:
                self.notes.append(
                    f"{MESSAGE_TABLE}.{column} traegt nicht ({total} Werte, kein "
                    f"Treffer in {table}); die Beziehung wird ueber die "
                    "Gegenrichtung gesucht."
                )

        # Gegenrichtung: <Tabelle>.ZMESSAGE -> ZMESSAGE.Z_PK
        for table in _DATA_TABLES:
            if not self.has(table, MESSAGE_TABLE) or not self.has(MESSAGE_TABLE, "Z_PK"):
                continue
            matched, total = self.connection.execute(
                f"SELECT COUNT(m.Z_PK), COUNT(d.{_quote(MESSAGE_TABLE)}) "
                f"FROM {_quote(table)} d "
                f"LEFT JOIN {_quote(MESSAGE_TABLE)} m ON m.Z_PK = d.{_quote(MESSAGE_TABLE)} "
                f"WHERE d.{_quote(MESSAGE_TABLE)} IS NOT NULL"
            ).fetchone()
            if total and matched:
                links.append(_Link(MESSAGE_TABLE, table, False, "data", matched, total))
            elif total:
                self.notes.append(
                    f"{table}.{MESSAGE_TABLE} ist vollstaendig verwaist "
                    f"({total} Werte, kein Treffer)."
                )

        return tuple(links)

    # -- Chats --------------------------------------------------------------

    def chat_map(self) -> dict[int, ChatReference]:
        """Konversations-Primaerschluessel -> Chat.

        Gruppen werden ueber `ZGROUPNAME` benannt, Einzelchats ueber den
        Kontakt: Vor- und Nachname, sonst der oeffentliche Nickname, sonst die
        Threema-ID. Ist nichts davon vorhanden, bleibt der Name None und der
        Export verwendet die Chat-ID - erfunden wird nichts.
        """
        if not self.has(CONVERSATION_TABLE, "Z_PK"):
            return {}

        contacts: dict[int, tuple[str | None, str]] = {}
        if self.has(CONTACT_TABLE, "Z_PK"):
            fields = [
                f
                for f in ("ZFIRSTNAME", "ZLASTNAME", "ZPUBLICNICKNAME", "ZIDENTITY")
                if self.has(CONTACT_TABLE, f)
            ]
            if fields:
                selection = ", ".join(_quote(f) for f in fields)
                for row in self.connection.execute(
                    f"SELECT Z_PK, {selection} FROM {_quote(CONTACT_TABLE)}"
                ):
                    values = dict(zip(fields, row[1:], strict=True))
                    first = (values.get("ZFIRSTNAME") or "").strip()
                    last = (values.get("ZLASTNAME") or "").strip()
                    full = " ".join(part for part in (first, last) if part)
                    nickname = (values.get("ZPUBLICNICKNAME") or "").strip()
                    identity = (values.get("ZIDENTITY") or "").strip()
                    name = full or nickname or identity or None
                    contacts[row[0]] = (name, "ZCONTACT")

        has_group_name = self.has(CONVERSATION_TABLE, "ZGROUPNAME")
        has_contact = self.has(CONVERSATION_TABLE, "ZCONTACT")
        selection = ["Z_PK"]
        if has_group_name:
            selection.append("ZGROUPNAME")
        if has_contact:
            selection.append("ZCONTACT")

        chats: dict[int, ChatReference] = {}
        for row in self.connection.execute(
            f"SELECT {', '.join(_quote(c) for c in selection)} "
            f"FROM {_quote(CONVERSATION_TABLE)}"
        ):
            values = dict(zip(selection, row, strict=True))
            pk = values["Z_PK"]
            group_name = (values.get("ZGROUPNAME") or "").strip() or None
            if group_name:
                chats[pk] = ChatReference(chat_id=str(pk), name=group_name, kind="group")
                continue
            contact_pk = values.get("ZCONTACT")
            name = contacts.get(contact_pk, (None, ""))[0] if contact_pk else None
            chats[pk] = ChatReference(
                chat_id=str(pk), name=name, kind="direct" if contact_pk else "unknown"
            )
        return chats

    # -- Nachrichten --------------------------------------------------------

    def message_map(self) -> dict[int, dict[str, object]]:
        """Nachrichten-Primaerschluessel -> Metadaten der Nachricht."""
        fields = [
            f
            for f in ("ZCONVERSATION", "ZFILENAME", "ZMIMETYPE", "ZDATE")
            if self.has(MESSAGE_TABLE, f)
        ]
        selection = ", ".join(_quote(f) for f in ["Z_PK", *fields])
        result: dict[int, dict[str, object]] = {}
        for row in self.connection.execute(
            f"SELECT {selection} FROM {_quote(MESSAGE_TABLE)}"
        ):
            values = dict(zip(["Z_PK", *fields], row, strict=True))
            result[values["Z_PK"]] = {
                "conversation": values.get("ZCONVERSATION"),
                "filename": (values.get("ZFILENAME") or None),
                "mime": (values.get("ZMIMETYPE") or None),
                "date": _apple_datetime(values.get("ZDATE")),
            }
        return result

    # -- Blobs --------------------------------------------------------------

    def blob_owners(
        self, links: tuple[_Link, ...]
    ) -> dict[tuple[str, int], tuple[int, bool, str]]:
        """(Tabelle, Z_PK) -> (Nachrichten-PK, ist_Vorschaubild, Belegkette)."""
        owners: dict[tuple[str, int], tuple[int, bool, str]] = {}

        for link in links:
            if link.direction == "message":
                rows = self.connection.execute(
                    f"SELECT m.Z_PK, m.{_quote(link.message_column)} "
                    f"FROM {_quote(MESSAGE_TABLE)} m "
                    f"JOIN {_quote(link.data_table)} d "
                    f"ON d.Z_PK = m.{_quote(link.message_column)}"
                )
            else:
                rows = self.connection.execute(
                    f"SELECT m.Z_PK, d.Z_PK FROM {_quote(link.data_table)} d "
                    f"JOIN {_quote(MESSAGE_TABLE)} m "
                    f"ON m.Z_PK = d.{_quote(MESSAGE_TABLE)}"
                )
            for message_pk, data_pk in rows:
                key = (link.data_table, data_pk)
                # Originale haben Vorrang vor Vorschaubildern, falls beides
                # auf denselben Blob zeigt.
                existing = owners.get(key)
                if existing is not None and not existing[1]:
                    continue
                owners[key] = (message_pk, link.is_thumbnail, link.evidence)
        return owners

    # -- Enumeration --------------------------------------------------------

    def enumerate(self) -> MediaEnumeration:
        """Baut die Liste der Medien aus Datenbank und Backup zusammen."""
        links = self.carrying_links()
        if not links:
            return MediaEnumeration(
                notes=tuple(self.notes),
                unsupported_reason=(
                    "In der Threema-Datenbank liess sich keine tragende "
                    "Beziehung zwischen Nachrichten und Medien nachweisen. Eine "
                    "Zuordnung waere geraten, deshalb wird keine vorgenommen."
                ),
            )

        chats = self.chat_map()
        messages = self.message_map()
        owners = self.blob_owners(links)

        items: list[MediaItem] = []
        #: Nachrichten-PK -> Kennung des Originals, fuer `thumbnail_of`.
        originals: dict[int, str] = {}
        thumbnails: list[tuple[int, int]] = []  # (Index in items, Nachrichten-PK)

        for table in _DATA_TABLES:
            if not self.has(table, "Z_PK", BLOB_COLUMN):
                continue
            for data_pk, blob in self.connection.execute(
                f"SELECT Z_PK, {_quote(BLOB_COLUMN)} FROM {_quote(table)}"
            ):
                if blob is None:
                    continue

                owner = owners.get((table, data_pk))
                message_pk = owner[0] if owner else None
                is_thumbnail = owner[1] if owner else False
                evidence = owner[2] if owner else None

                message = messages.get(message_pk) if message_pk is not None else None
                chat = None
                if message is not None:
                    conversation = message.get("conversation")
                    if conversation is not None:
                        chat = chats.get(conversation)

                item = self._build_item(
                    table=table,
                    data_pk=data_pk,
                    blob=blob,
                    chat=chat,
                    message=message,
                    message_pk=message_pk,
                    is_thumbnail=is_thumbnail,
                    evidence=evidence,
                )
                if item is None:
                    continue

                if is_thumbnail:
                    if message_pk is not None:
                        thumbnails.append((len(items), message_pk))
                elif message_pk is not None:
                    originals.setdefault(message_pk, item.source.identity())
                items.append(item)

        # Zweiter Durchgang: Vorschaubilder mit ihrem Original verknuepfen.
        for index, message_pk in thumbnails:
            original = originals.get(message_pk)
            if original is None:
                continue
            item = items[index]
            items[index] = replace(item, thumbnail_of=original)

        if self.dangling:
            self.notes.append(
                f"{self.dangling} Blob-Referenzen zeigen auf eine Datei, die im "
                "Backup nicht vorhanden ist. Diese Medien sind nicht extrahierbar."
            )
        if self.unexpected_prefixes:
            self.notes.append(
                f"{self.unexpected_prefixes} Inline-Blobs beginnen nicht mit dem "
                f"erwarteten Core-Data-Markierungsbyte 0x{INLINE_PREFIX:02X}. Bei "
                "ihnen wurde nichts abgeschnitten; sie koennen um ein Byte "
                "verschoben sein."
            )

        return MediaEnumeration(
            items=tuple(items),
            dangling_references=self.dangling,
            notes=tuple(self.notes),
        )

    def _build_item(
        self,
        *,
        table: str,
        data_pk: int,
        blob: bytes | memoryview,
        chat: ChatReference | None,
        message: dict[str, object] | None,
        message_pk: int | None,
        is_thumbnail: bool,
        evidence: str | None,
    ) -> MediaItem | None:
        """Erzeugt einen `MediaItem` aus einer Blob-Zeile.

        Gibt None zurueck, wenn die Zeile auf eine Datei zeigt, die im Backup
        fehlt - dann ist nichts zu extrahieren.
        """
        filename = message.get("filename") if message else None
        timestamp = message.get("date") if message else None
        mime = message.get("mime") if message else None

        if is_external_reference(blob):
            name = external_reference_name(blob)
            entry = self.context.external_files.get(name)
            if entry is None:
                self.dangling += 1
                return None
            source = MediaSource(
                kind=SourceKind.EXTERNAL_FILE,
                file_id=entry.file_id,
                domain=entry.domain,
                relative_path=entry.relative_path,
            )
            size = entry.size
        else:
            raw = bytes(blob)
            if raw[:1] == bytes([INLINE_PREFIX]):
                offset = INLINE_PREFIX_LENGTH
            else:
                # Unerwartete Markierung: nicht abschneiden, sondern melden.
                # Ein falsch abgeschnittenes Byte waere schlimmer als ein Byte
                # zu viel, weil es unbemerkt bliebe.
                offset = 0
                self.unexpected_prefixes += 1
            source = MediaSource(
                kind=SourceKind.INLINE_BLOB,
                table=table,
                row_id=data_pk,
                column=BLOB_COLUMN,
                byte_offset=offset,
            )
            size = max(0, len(raw) - offset)

        return MediaItem(
            source=source,
            size=size,
            chat=chat,
            original_filename=filename if isinstance(filename, str) else None,
            timestamp=timestamp if isinstance(timestamp, datetime) else None,
            declared_mime=mime if isinstance(mime, str) else None,
            is_thumbnail=is_thumbnail,
            message_id=str(message_pk) if message_pk is not None else None,
            evidence=evidence,
        )
