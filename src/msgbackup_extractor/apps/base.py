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
class ChatAssignment:
    """Zuordnung einer Datei zu einem Chat - nur bei belegbarer Verknuepfung."""

    file_id: str
    chat_name: str
    chat_id: str
    #: Woran die Zuordnung haengt, z.B. "messages.media_id -> media.id".
    evidence: str


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

    def link_media(
        self,
        connection: sqlite3.Connection,
        entries: tuple[ManifestEntry, ...],
    ) -> tuple[ChatAssignment, ...]:
        """Ordnet Medien Chats zu - nur bei belegbarer Verknuepfung.

        Die Standardimplementierung ordnet nichts zu. Ein Profil ueberschreibt
        das erst, wenn das Schema der App bekannt und belegt ist. Alles, was
        hier nicht zurueckkommt, landet im Export unter `unassigned/`.
        """
        return ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(slug={self.slug!r})"
