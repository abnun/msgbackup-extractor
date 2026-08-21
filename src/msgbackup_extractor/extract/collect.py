"""Ausgewaehlte Dateien eines Exports in ein Zielverzeichnis sammeln.

Warum das die CLI macht und nicht der Browser: JavaScript auf einer
`file://`-Seite darf lokale Dateien **anzeigen**, ihre Bytes aber nicht lesen.
`fetch` und `XMLHttpRequest` scheitern an der Same-Origin-Regel, und ein
`canvas`, in das ein lokales Bild gezeichnet wurde, ist "tainted" und laesst
sich nicht auslesen. Am echten Export nachgemessen:

    fetch:            BLOCKIERT (TypeError: Failed to fetch)
    XMLHttpRequest:   BLOCKIERT
    <img> anzeigen:   OK
    canvas auslesen:  BLOCKIERT (SecurityError)

Ein ZIP im Browser ist damit unmoeglich. Das UI liefert deshalb nur die Liste
der ausgewaehlten Pfade; hier werden die Dateien tatsaechlich zusammengetragen.

Standardmaessig per Hardlink: eine Sammlung von 2 GB belegt dann keinen
zusaetzlichen Speicher. Wer die Dateien weitergeben will, nimmt `--no-hardlinks`
oder packt die Sammlung anschliessend selbst.
"""

from __future__ import annotations

import enum
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from msgbackup_extractor.core.hashing import compare, hash_file
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.paths import OutputGuard, sanitize_component
from msgbackup_extractor.extract.export_manifest import LoadedManifest

logger = get_logger("collect")

#: Zeilen, die als Kommentar gelten - damit sich eine Liste kommentieren laesst.
_COMMENT_PREFIXES: Final = ("#", "//")


class CollectOutcome(enum.StrEnum):
    """Ergebnis fuer eine einzelne Datei."""

    COLLECTED = "collected"
    #: Bereits im Ziel vorhanden und inhaltsgleich.
    SKIPPED = "skipped"
    #: Nicht im Export-Manifest - der Pfad gehoert nicht zu diesem Export.
    UNKNOWN = "unknown"
    #: Im Manifest, aber die Datei fehlt auf der Platte.
    MISSING = "missing"
    #: Hash weicht vom Manifest ab.
    CORRUPT = "corrupt"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectedFile:
    """Ergebnis fuer eine Datei."""

    source: str
    outcome: CollectOutcome
    target: str | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.outcome in (CollectOutcome.COLLECTED, CollectOutcome.SKIPPED)


@dataclass(frozen=True, slots=True)
class CollectResult:
    """Gesamtergebnis."""

    files: tuple[CollectedFile, ...]
    target_dir: Path
    hardlinked: bool
    verified: bool

    def count(self, outcome: CollectOutcome) -> int:
        return sum(1 for f in self.files if f.outcome is outcome)

    @property
    def collected(self) -> int:
        return self.count(CollectOutcome.COLLECTED)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if not f.is_ok)

    @property
    def total_bytes(self) -> int:
        return sum(f.size or 0 for f in self.files if f.is_ok)

    @property
    def problems(self) -> tuple[CollectedFile, ...]:
        return tuple(f for f in self.files if not f.is_ok)


@dataclass(frozen=True, slots=True)
class CollectOptions:
    """Wie gesammelt wird."""

    #: Hardlinks statt Kopien - kostet keinen zusaetzlichen Speicher.
    hardlinks: bool = True
    #: Verzeichnisstruktur des Exports beibehalten statt flach zu sammeln.
    keep_structure: bool = False
    #: SHA-256 gegen das Manifest pruefen. Kostet Lesezeit, gibt Sicherheit.
    verify: bool = False
    dry_run: bool = False


def parse_selection(text: str) -> list[str]:
    """Liest eine Auswahlliste: ein relativer Pfad je Zeile.

    Leerzeilen und Kommentarzeilen werden uebergangen, damit eine Liste
    kommentiert und von Hand nachbearbeitet werden kann. Doppelte Eintraege
    werden zusammengefasst, die Reihenfolge bleibt erhalten.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(_COMMENT_PREFIXES):
            continue
        # Fuehrende "./" und umgebende Anfuehrungszeichen abstreifen.
        candidate = candidate.strip("\"'").removeprefix("./")
        if candidate and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _target_name(relative: str, *, keep_structure: bool) -> PurePosixPath:
    """Zielpfad innerhalb des Sammelverzeichnisses."""
    source = PurePosixPath(relative)
    if keep_structure:
        return PurePosixPath(*(sanitize_component(part) for part in source.parts))
    return PurePosixPath(sanitize_component(source.name))


@dataclass(slots=True)
class Collector:
    """Traegt ausgewaehlte Dateien eines Exports zusammen."""

    manifest: LoadedManifest
    target_dir: Path
    options: CollectOptions = field(default_factory=CollectOptions)

    def run(self, selection: list[str]) -> CollectResult:
        export_dir = self.manifest.output_dir
        known = {
            entry.output_path: entry
            for entry in self.manifest.files
            if entry.output_path
        }
        # Auch die Zusatzpfade der Chat-Struktur sind gueltige Auswahlen.
        for entry in self.manifest.files:
            for link in entry.link_paths:
                known.setdefault(link, entry)

        guard = OutputGuard(
            root=self.target_dir,
            forbidden_roots=(export_dir,),
            forbidden_label="Export",
        )
        results: list[CollectedFile] = []
        # Was schon im Ziel liegt, ist belegt. Ohne das ueberschreibt ein
        # zweiter Lauf eine gleichnamige Datei des ersten - und das ist kein
        # Sonderfall, sondern der Regelfall, sobald eine grosse Auswahl auf
        # mehrere Aufrufe verteilt wird.
        used: set[PurePosixPath] = self._existing_names()

        for relative in selection:
            entry = known.get(relative)
            if entry is None:
                logger.warning("Pfad gehoert nicht zu diesem Export, uebersprungen")
                results.append(
                    CollectedFile(source=relative, outcome=CollectOutcome.UNKNOWN)
                )
                continue

            source = export_dir / relative
            if not source.is_file():
                results.append(
                    CollectedFile(
                        source=relative, outcome=CollectOutcome.MISSING, size=entry.size
                    )
                )
                continue

            if self.options.verify and entry.sha256:
                actual = hash_file(source)
                if not compare(actual, entry.sha256):
                    logger.error("Hash weicht vom Manifest ab: %s", PurePosixPath(relative).name)
                    results.append(
                        CollectedFile(
                            source=relative,
                            outcome=CollectOutcome.CORRUPT,
                            size=entry.size,
                            sha256=entry.sha256,
                        )
                    )
                    continue

            destination = self._unique(
                _target_name(relative, keep_structure=self.options.keep_structure),
                used,
                source,
            )
            if self.options.dry_run:
                results.append(
                    CollectedFile(
                        source=relative,
                        outcome=CollectOutcome.COLLECTED,
                        target=str(destination),
                        size=entry.size,
                        sha256=entry.sha256,
                    )
                )
                continue

            try:
                target = guard.prepare(destination)
                self._place(source, target)
            except OSError as error:
                logger.warning("Sammeln fehlgeschlagen: %s", type(error).__name__)
                results.append(
                    CollectedFile(
                        source=relative,
                        outcome=CollectOutcome.FAILED,
                        error=type(error).__name__,
                    )
                )
                continue

            results.append(
                CollectedFile(
                    source=relative,
                    outcome=CollectOutcome.COLLECTED,
                    target=str(destination),
                    size=entry.size,
                    sha256=entry.sha256,
                )
            )

        return CollectResult(
            files=tuple(results),
            target_dir=self.target_dir,
            hardlinked=self.options.hardlinks,
            verified=self.options.verify,
        )

    def _place(self, source: Path, target: Path) -> None:
        """Legt die Datei ab - als Hardlink, sonst als Kopie."""
        if target.exists():
            target.unlink()
        if self.options.hardlinks:
            try:
                os.link(source, target)
                return
            except OSError as error:
                # Hardlinks gehen nicht ueber Dateisystemgrenzen.
                logger.debug("Hardlink nicht moeglich (%s), kopiere", type(error).__name__)
        shutil.copy2(source, target)

    def _existing_names(self) -> set[PurePosixPath]:
        """Namen, die im Zielverzeichnis schon vergeben sind."""
        if not self.target_dir.exists():
            return set()
        namen: set[PurePosixPath] = set()
        for pfad in self.target_dir.rglob("*"):
            if pfad.is_file():
                namen.add(PurePosixPath(pfad.relative_to(self.target_dir).as_posix()))
        return namen

    def _unique(
        self,
        candidate: PurePosixPath,
        used: set[PurePosixPath],
        source: Path | None = None,
    ) -> PurePosixPath:
        """Vermeidet Kollisionen beim flachen Sammeln, statt zu ueberschreiben.

        Eine Ausnahme: liegt unter dem Namen schon **dieselbe** Datei - bei
        Hardlinks derselbe Inode -, dann ist sie bereits eingesammelt. Sonst
        wuerde derselbe Aufruf zweimal ausgefuehrt Kopien anlegen.
        """
        if candidate in used and source is not None:
            vorhanden = self.target_dir / candidate
            try:
                if vorhanden.exists() and os.path.samefile(vorhanden, source):
                    return candidate
            except OSError:
                pass
        if candidate not in used:
            used.add(candidate)
            return candidate
        stem, suffix = PurePosixPath(candidate.name).stem, PurePosixPath(candidate.name).suffix
        for counter in range(1, 10_000):
            alternative = candidate.parent / f"{stem}-{counter}{suffix}"
            if alternative not in used:
                used.add(alternative)
                return alternative
        raise ValueError(f"Zu viele Namenskollisionen fuer {candidate.name}")
