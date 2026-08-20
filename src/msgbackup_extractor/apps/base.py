"""Basis fuer Messenger-Profile.

Ein Profil beschreibt, wie eine App im Backup zu erkennen ist - nicht, wo ihre
Dateien liegen. Der Unterschied ist wesentlich: Pfade aendern sich zwischen
App-Versionen, der Bundle Identifier nicht.

Die Erkennung laeuft in zwei Stufen, weil unterschiedlich viel Zugriff noetig ist:

1. `detect()` arbeitet nur mit `Info.plist` und `Manifest.plist`. Das
   funktioniert auch bei verschluesselten Backups **ohne Passwort**.
2. `match_domains()` braucht die Domains aus der Manifest.db und damit bei
   verschluesselten Backups das Passwort.

Erkannt wird ueber ein Muster, nicht ueber eine Liste fest verdrahteter
Bezeichner: ein Profil nennt einen Namensraum (z.B. `ch.threema.`), und das
Profil findet darin alle tatsaechlich vorhandenen Varianten. Bestaetigt gilt ein
Kandidat nur, wenn er in den Backup-Metadaten wirklich auftaucht.
"""

from __future__ import annotations

import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from msgbackup_extractor.models import (
    BackupInfo,
    DetectionResult,
    DetectionStatus,
    DomainMatch,
    ManifestEntry,
    MediaItem,
    TableSchema,
)

#: Praefixe, mit denen iOS App-Domains im Backup benennt.
DOMAIN_PREFIXES: Final[dict[str, str]] = {
    "AppDomain-": "app",
    "AppDomainGroup-": "group",
    "AppDomainPlugin-": "plugin",
}


def domain_kind(domain: str) -> str:
    """Klassifiziert eine Domain anhand ihres Praefixes."""
    for prefix, kind in DOMAIN_PREFIXES.items():
        if domain.startswith(prefix):
            return kind
    return "unknown"


def domain_identifier(domain: str) -> str:
    """Der Teil hinter dem Praefix, z.B. `ch.threema.iapp` oder `group.ch.threema`."""
    for prefix in DOMAIN_PREFIXES:
        if domain.startswith(prefix):
            return domain[len(prefix) :]
    return domain


@dataclass(frozen=True, slots=True)
class DatabaseCandidate:
    """Eine im Backup gefundene SQLite-Datenbank der App."""

    entry: ManifestEntry
    #: Nach `sqlite_ro.describe_database()` gefuellt, sonst leer.
    tables: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseRole:
    """Einschaetzung, welche Rolle eine Datenbank spielt."""

    candidate: DatabaseCandidate
    #: "messages", "contacts", "media", "metadata" oder "unknown".
    role: str
    #: Begruendung fuer den Bericht; bei "unknown" der Grund fuer die Unsicherheit.
    reason: str
    confidence: str = "low"


@dataclass(frozen=True, slots=True)
class MediaContext:
    """Alles, was ein Profil braucht, um Medien aufzuzaehlen."""

    #: Read-only-Verbindung zur App-Datenbank.
    connection: sqlite3.Connection
    #: Schema der App-Datenbank, damit das Profil nichts voraussetzen muss.
    schemas: dict[str, TableSchema]
    #: Dateiname im externen Blob-Verzeichnis -> Manifest-Eintrag.
    #: Damit loest ein Profil eine Blob-Referenz auf eine Backupdatei auf.
    external_files: dict[str, ManifestEntry]
    #: Alle Manifest-Eintraege der App, nach relativem Pfad.
    entries_by_path: dict[str, ManifestEntry]


@dataclass(frozen=True, slots=True)
class MediaEnumeration:
    """Ergebnis von `AppProfile.enumerate_media()`."""

    items: tuple[MediaItem, ...] = ()
    #: Referenzen, die auf keine Datei im Backup zeigen.
    dangling_references: int = 0
    #: Menschenlesbare Hinweise fuer den Bericht.
    notes: tuple[str, ...] = ()
    #: Gesetzt, wenn das Schema nicht unterstuetzt wird. Dann ist `items` leer
    #: und der Aufrufer erzeugt einen Diagnosebericht statt zu raten.
    unsupported_reason: str | None = None

    @property
    def is_supported(self) -> bool:
        return self.unsupported_reason is None


class AppProfile(ABC):
    """Erkennungsprofil fuer einen Messenger."""

    #: Anzeigename, z.B. "Threema".
    name: str = "Unbekannt"

    #: Kurzname fuer `--app` und Verzeichnisnamen, z.B. "threema".
    slug: str = "unbekannt"

    #: Namensraum der Bundle Identifier, z.B. "ch.threema.". Wird als Praefix
    #: verwendet, damit auch Varianten (Work, onPrem) gefunden werden, ohne dass
    #: deren exakte Bezeichner fest verdrahtet sein muessen.
    bundle_namespaces: tuple[str, ...] = ()

    #: Bekannte Bezeichner. Dienen nur als Hinweis fuer die Diagnose - ein
    #: Eintrag hier gilt erst als erkannt, wenn er im Backup vorkommt.
    known_bundle_ids: tuple[str, ...] = ()

    #: Zusaetzliche Muster fuer App-Group-Container, die den Bundle Identifier
    #: nicht vollstaendig enthalten.
    group_namespaces: tuple[str, ...] = ()

    # -- Stufe 1: Erkennung ueber Metadaten ---------------------------------

    def matches_bundle_id(self, bundle_id: str) -> bool:
        """Gehoert ein Bundle Identifier zu dieser App?"""
        if bundle_id in self.known_bundle_ids:
            return True
        return any(bundle_id.startswith(namespace) for namespace in self.bundle_namespaces)

    def detect(self, info: BackupInfo) -> DetectionResult:
        """Sucht die App in den Backup-Metadaten.

        Es wird nichts angenommen: gefunden werden nur Bezeichner, die in
        `Manifest.plist:Applications` oder `Info.plist:Installed Applications`
        tatsaechlich stehen.
        """
        matches = [app for app in info.applications if self.matches_bundle_id(app.bundle_id)]
        candidates = tuple(app.bundle_id for app in matches)

        if not matches:
            return DetectionResult(
                app_name=self.name,
                status=DetectionStatus.NOT_FOUND,
                candidates=(),
                reason=(
                    f"Kein Bundle Identifier im Namensraum "
                    f"{', '.join(self.bundle_namespaces) or '(keiner definiert)'} "
                    f"gefunden. Das Backup nennt {len(info.applications)} "
                    f"{'App' if len(info.applications) == 1 else 'Apps'}."
                ),
            )

        # Bestaetigte Installationen haben Vorrang vor blossen Manifest-Eintraegen.
        confirmed = [app for app in matches if app.confirmed_installed]
        pool = confirmed or matches

        if len(pool) > 1:
            return DetectionResult(
                app_name=self.name,
                status=DetectionStatus.AMBIGUOUS,
                candidates=candidates,
                reason=(
                    f"Mehrere {self.name}-Varianten im Backup: "
                    f"{', '.join(app.bundle_id for app in pool)}. "
                    "Waehle eine davon mit --bundle-id aus."
                ),
            )

        app = pool[0]
        return DetectionResult(
            app_name=self.name,
            status=DetectionStatus.CONFIRMED,
            bundle_id=app.bundle_id,
            bundle_version=app.bundle_version,
            candidates=candidates,
            reason=(
                None
                if app.confirmed_installed
                else (
                    "Der Bezeichner steht in Manifest.plist, aber nicht in "
                    "Info.plist:Installed Applications. Die App war zum "
                    "Backup-Zeitpunkt womoeglich nicht mehr installiert."
                )
            ),
        )

    # -- Stufe 2: Domains ---------------------------------------------------

    def domain_patterns(self, bundle_id: str) -> tuple[re.Pattern[str], ...]:
        """Muster, mit denen die Domains der App im Manifest gefunden werden.

        Standardimplementierung: alles, dessen Identifier den Bundle Identifier
        oder einen der Namensraeume enthaelt. Das erfasst App-Container,
        App-Group-Container und Extension-Plugins, ohne deren genaue Namen zu
        kennen.
        """
        needles = {re.escape(bundle_id)}
        needles.update(re.escape(namespace.rstrip(".")) for namespace in self.bundle_namespaces)
        needles.update(re.escape(namespace) for namespace in self.group_namespaces)
        return tuple(re.compile(rf"(?:^|\.){needle}(?:$|\.)") for needle in sorted(needles))

    def match_domains(
        self,
        bundle_id: str,
        available_domains: tuple[str, ...],
    ) -> tuple[DomainMatch, ...]:
        """Waehlt aus den vorhandenen Domains die zur App gehoerenden aus."""
        patterns = self.domain_patterns(bundle_id)
        found: list[DomainMatch] = []
        for domain in available_domains:
            identifier = domain_identifier(domain)
            if any(pattern.search(identifier) for pattern in patterns):
                found.append(DomainMatch(domain=domain, kind=domain_kind(domain)))
        return tuple(found)

    # -- Stufe 3: Datenbanken und Chatzuordnung -----------------------------

    @abstractmethod
    def classify_databases(
        self, candidates: tuple[DatabaseCandidate, ...]
    ) -> tuple[DatabaseRole, ...]:
        """Ordnet gefundene Datenbanken Rollen zu.

        Muss bei Unsicherheit `role="unknown"` mit Begruendung liefern, damit der
        Diagnosebericht aussagekraeftig bleibt.
        """

    def enumerate_media(self, context: MediaContext) -> MediaEnumeration:
        """Zaehlt die Medien auf, die die App-Datenbank kennt.

        Hier entsteht die Verbindung zwischen Datenbank und Dateien: welche
        Blobs es gibt, wo ihr Inhalt liegt (Datei im Backup oder inline in der
        Datenbank), zu welchem Chat sie gehoeren, wie sie im Original hiessen
        und wann sie entstanden sind.

        Die Standardimplementierung zaehlt nichts auf. Ein Profil ueberschreibt
        sie erst, wenn das Schema der App vermessen und belegt ist - nicht auf
        Grundlage von Vermutungen. Ohne Ueberschreibung faellt der Export auf
        die reine Dateiauswahl anhand der Domains zurueck.
        """
        return MediaEnumeration(
            unsupported_reason=(
                f"Fuer {self.name} ist keine Medien-Zuordnung implementiert. "
                "Der Export erfolgt anhand der Domains, ohne Chat-Struktur."
            )
        )

    def requires_tables(self) -> tuple[str, ...]:
        """Tabellen, ohne die `enumerate_media()` nicht arbeiten kann."""
        return ()

    def supports_schema(self, schemas: dict[str, TableSchema]) -> str | None:
        """Prueft das Schema. Gibt None zurueck, wenn es traegt, sonst den Grund."""
        available = {name.upper() for name in schemas}
        missing = [t for t in self.requires_tables() if t.upper() not in available]
        if missing:
            return (
                f"In der Datenbank fehlen die Tabellen {', '.join(missing)}. "
                f"Vorhanden sind {len(schemas)} Tabellen. Ohne diese ist keine "
                "Zuordnung von Medien zu Chats moeglich."
            )
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(slug={self.slug!r})"
