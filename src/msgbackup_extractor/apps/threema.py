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

from typing import Final

from msgbackup_extractor.apps.base import AppProfile, DatabaseCandidate, DatabaseRole

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
