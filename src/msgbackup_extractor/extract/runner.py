"""Ausfuehrung der Extraktion.

Grundsaetze:

* **Jede Datei ist eine eigene Transaktion.** Ein Fehler bei einer Datei
  erzeugt einen Eintrag im Bericht und eine Warnung, dann geht es weiter. Ein
  beschaedigtes Video darf nicht tausende andere Dateien kosten.
* **Integritaet wird geprueft, nicht behauptet.** Der SHA-256 des Quellinhalts
  entsteht beim Schreiben im Vorbeigehen, der des Ziels wird danach aus der
  geschriebenen Datei gelesen. Erst der Vergleich beider Werte gilt als Nachweis.
* **Geschrieben wird nur durch den Output-Guard.** Jeder Zielpfad wird gegen das
  Ausgabeverzeichnis verankert und gegen das Backup geprueft.
* **Unvollstaendiges wird aufgeraeumt.** Bricht das Schreiben ab, wird die
  Teildatei entfernt, damit kein halbes Video wie ein Erfolg aussieht.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from msgbackup_extractor.core import media as media_module
from msgbackup_extractor.core.hashing import hash_file
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.paths import OutputGuard
from msgbackup_extractor.extract.planner import ExtractOptions
from msgbackup_extractor.extract.sources import (
    MediaReader,
    SourceMissing,
    SourceUnavailable,
    SourceUndecryptable,
)
from msgbackup_extractor.models import (
    ExtractedFile,
    ExtractionPlan,
    ExtractionResult,
    FileOutcome,
    PlannedFile,
)

logger = get_logger("runner")


@dataclass(slots=True)
class Runner:
    """Fuehrt einen Extraktionsplan aus."""

    reader: MediaReader
    guard: OutputGuard
    options: ExtractOptions = field(default_factory=ExtractOptions)
    #: Wird bei jedem Fortschritt aufgerufen: (erledigt, gesamt).
    progress: object | None = None

    def run(self, plan: ExtractionPlan) -> ExtractionResult:
        results: list[ExtractedFile] = []
        #: Inhaltshash -> bereits geschriebener Ausgabepfad, fuer Duplikate.
        seen: dict[str, str] = {}
        total = plan.total_files

        for index, planned in enumerate(plan.files, start=1):
            results.append(self._process(planned, seen))
            if callable(self.progress):
                self.progress(index, total)

        for item in plan.missing:
            results.append(
                ExtractedFile(
                    outcome=FileOutcome.MISSING,
                    source_kind=item.source.kind,
                    output_path=None,
                    source_file_id=item.source.file_id,
                    source_table=item.source.table,
                    source_row_id=item.source.row_id,
                    chat_name=item.chat.name if item.chat else None,
                    chat_id=item.chat.chat_id if item.chat else None,
                    error="source_missing",
                )
            )

        return ExtractionResult(
            files=tuple(results),
            dry_run=self.options.dry_run,
            output_dir=self.guard.root,
        )

    # -- Einzelne Datei -----------------------------------------------------

    def _process(self, planned: PlannedFile, seen: dict[str, str]) -> ExtractedFile:
        item = planned.item
        base = self._describe(planned)

        if self.options.dry_run:
            # Kein Lesen des vollen Inhalts: bei 3 GB waere ein Probelauf sonst
            # so teuer wie der echte. Deshalb hier auch kein Inhaltshash und
            # keine Duplikaterkennung - der Bericht weist das aus.
            return base

        try:
            target = self.guard.prepare(planned.output_path)
            source_hash, written, head = self._write(planned, target)
        except SourceMissing:
            logger.warning("Quelle fehlt, wird uebersprungen (fileID %s)",
                           (item.source.file_id or "-")[:8])
            return self._with(base, outcome=FileOutcome.MISSING, error="source_missing")
        except SourceUndecryptable:
            logger.warning("Nicht entschluesselbar, wird uebersprungen")
            return self._with(
                base, outcome=FileOutcome.UNDECRYPTABLE, error="undecryptable"
            )
        except SourceUnavailable as error:
            logger.warning("Quelle nicht lesbar: %s", type(error).__name__)
            return self._with(base, outcome=FileOutcome.FAILED,
                              error=type(error).__name__)
        except OSError as error:
            logger.warning("Schreibfehler: %s", type(error).__name__)
            return self._with(base, outcome=FileOutcome.FAILED,
                              error=type(error).__name__)

        # Duplikat? Nicht loeschen, nur markieren - und mit --deduplicate den
        # zweiten Pfad gar nicht behalten.
        duplicate_of = seen.get(source_hash)
        if duplicate_of is not None and self.options.deduplicate:
            target.unlink(missing_ok=True)
            return self._with(
                base,
                outcome=FileOutcome.DUPLICATE,
                sha256=source_hash,
                size=written,
                output_path=duplicate_of,
                duplicate_of=duplicate_of,
                link_paths=(),
            )

        destination_hash = hash_file(target)
        if destination_hash != source_hash:
            logger.error(
                "Integritaetspruefung fehlgeschlagen fuer %s",
                planned.output_path.name,
            )
            return self._with(
                base,
                outcome=FileOutcome.INTEGRITY_ERROR,
                sha256=source_hash,
                size=written,
                integrity_ok=False,
                error="hash_mismatch",
            )

        links = self._create_links(planned, target)
        if duplicate_of is None:
            seen[source_hash] = str(planned.output_path)

        measured = media_module.dimensions(head)
        return self._with(
            base,
            outcome=FileOutcome.EXTRACTED,
            sha256=source_hash,
            size=written,
            integrity_ok=True,
            link_paths=links,
            duplicate_of=duplicate_of,
            width=measured[0] if measured else None,
            height=measured[1] if measured else None,
        )

    def _write(self, planned: PlannedFile, target: Path) -> tuple[str, int, bytes]:
        """Schreibt den Inhalt und bildet dabei den Quellhash.

        Der Hash entsteht aus denselben Bytes, die geschrieben werden - er kann
        also nicht versehentlich zu einer anderen Fassung der Daten gehoeren.

        Der Anfang der Daten wird zusaetzlich gemerkt, damit die Pixelmasse
        daraus gelesen werden koennen. Das kostet kein zusaetzliches Lesen: die
        Bytes laufen hier ohnehin durch.
        """
        digest = hashlib.sha256()
        written = 0
        head = bytearray()
        try:
            with target.open("wb") as handle:
                for chunk in self.reader.stream(planned.item):
                    digest.update(chunk)
                    handle.write(chunk)
                    written += len(chunk)
                    if len(head) < media_module.DIMENSION_SCAN_LIMIT:
                        head += chunk[: media_module.DIMENSION_SCAN_LIMIT - len(head)]
        except BaseException:
            # Teildatei entfernen: sie waere sonst ein stiller Teilverlust.
            target.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), written, bytes(head)

    def _create_links(self, planned: PlannedFile, target: Path) -> tuple[str, ...]:
        """Legt die zusaetzlichen Pfade an - als Hardlink, sonst als Kopie."""
        created: list[str] = []
        for relative in planned.link_paths:
            try:
                link = self.guard.prepare(relative)
            except ValueError as error:
                logger.warning("Zielpfad abgelehnt: %s", type(error).__name__)
                continue
            if link.exists():
                continue
            try:
                if self.options.hardlinks:
                    os.link(target, link)
                else:
                    shutil.copy2(target, link)
            except OSError as error:
                # Hardlinks gehen nicht ueber Dateisystemgrenzen und nicht auf
                # jedem Dateisystem. Dann eben kopieren.
                logger.debug("Hardlink nicht moeglich (%s), kopiere", type(error).__name__)
                try:
                    shutil.copy2(target, link)
                except OSError as copy_error:
                    logger.warning("Verknuepfung fehlgeschlagen: %s",
                                   type(copy_error).__name__)
                    continue
            created.append(str(relative))
        return tuple(created)

    # -- Berichtseintraege --------------------------------------------------

    def _describe(self, planned: PlannedFile) -> ExtractedFile:
        item = planned.item
        media_type = planned.media_type
        return ExtractedFile(
            outcome=FileOutcome.EXTRACTED,
            source_kind=item.source.kind,
            output_path=str(planned.output_path),
            link_paths=tuple(str(p) for p in planned.link_paths),
            size=item.size,
            media_type=media_type.mime_type if media_type else None,
            detection_method=media_type.detection_method.value if media_type else None,
            extension_mismatch=bool(media_type and media_type.extension_mismatch),
            source_file_id=item.source.file_id,
            source_domain=item.source.domain,
            source_table=item.source.table,
            source_row_id=item.source.row_id,
            chat_name=item.chat.name if item.chat else None,
            chat_id=item.chat.chat_id if item.chat else None,
            original_filename=item.original_filename,
            timestamp=item.timestamp,
            timestamp_source=item.timestamp_source,
            is_thumbnail=item.is_thumbnail,
            thumbnail_of=item.thumbnail_of,
        )

    @staticmethod
    def _with(base: ExtractedFile, **changes: object) -> ExtractedFile:
        from dataclasses import replace

        return replace(base, **changes)  # type: ignore[arg-type]


def relative_to_output(path: Path, root: Path) -> PurePosixPath:
    """Pfad relativ zum Ausgabeverzeichnis, mit `/` als Trenner."""
    return PurePosixPath(path.relative_to(root).as_posix())
