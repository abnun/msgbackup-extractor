"""Planung der Extraktion.

Der Planer rechnet, er schreibt nicht. Er liest die Dateikoepfe, um Medientypen
zu bestimmen, und legt daraus die Zielpfade fest. Ergebnis ist ein
`ExtractionPlan`.

Der Nutzen dieser Trennung: `--dry-run` und der echte Lauf verwenden **denselben
Plan**. Ein Probelauf kann sich damit nicht anders verhalten als der Ernstfall,
weil es keine zweite Codepfad-Variante gibt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final

from msgbackup_extractor.core.hashing import hash_bytes
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.paths import sanitize_component
from msgbackup_extractor.extract.sources import MediaReader, SourceUnavailable
from msgbackup_extractor.models import (
    ExtractionPlan,
    MediaCategory,
    MediaItem,
    MediaType,
    PlannedFile,
)

logger = get_logger("planner")

MEDIA_DIR: Final = "media"
CHATS_DIR: Final = "chats"
THUMBNAIL_DIR: Final = "thumbnails"
UNASSIGNED: Final = "unassigned"

#: Kategorien, die als Nutzmedien gelten. Datenbanken und Sonstiges werden
#: getrennt behandelt, damit App-Interna nicht zwischen den Fotos landen.
USER_MEDIA: Final = frozenset(
    {
        MediaCategory.IMAGE,
        MediaCategory.VIDEO,
        MediaCategory.AUDIO,
        MediaCategory.DOCUMENT,
        MediaCategory.ARCHIVE,
    }
)

#: Formate, die im Threema-Container vorkommen, aber App-Interna sind.
#: Sie gehen nach `metadata/`, nicht in die Medienverzeichnisse.
APP_INTERNAL_FORMATS: Final = frozenset({"PLIST", "TEXT", "LOG"})


@dataclass(frozen=True, slots=True)
class ExtractOptions:
    """Was der Nutzer fuer diesen Lauf gewaehlt hat."""

    include_thumbnails: bool = True
    organize_by_chat: bool = True
    #: Chat-Struktur ueber Hardlinks statt Kopien - kostet keinen Speicher.
    hardlinks: bool = True
    deduplicate: bool = False
    dry_run: bool = False
    #: Wenn gesetzt, werden nur diese Kategorien exportiert.
    categories: frozenset[MediaCategory] | None = None


@dataclass(slots=True)
class Planner:
    """Baut den Extraktionsplan."""

    reader: MediaReader
    options: ExtractOptions = field(default_factory=ExtractOptions)

    def build(self, items: tuple[MediaItem, ...]) -> ExtractionPlan:
        planned: list[PlannedFile] = []
        excluded: list[tuple[MediaItem, str]] = []
        missing: list[MediaItem] = []
        used_paths: set[PurePosixPath] = set()

        for item in items:
            if item.is_thumbnail and not self.options.include_thumbnails:
                excluded.append((item, "Vorschaubild, per Option ausgeschlossen"))
                continue

            try:
                media_type = self.reader.detect_type(item)
            except SourceUnavailable as error:
                # Eine einzelne unlesbare oder fehlende Datei darf die Planung
                # nicht abbrechen. Sie wird vermerkt und der Lauf geht weiter.
                logger.debug("Quelle nicht verwertbar: %s", type(error).__name__)
                missing.append(item)
                continue

            if (
                self.options.categories is not None
                and media_type.category not in self.options.categories
            ):
                excluded.append(
                    (item, f"Kategorie {media_type.category.value} nicht angefordert")
                )
                continue

            output_path = self._unique(
                self._media_path(item, media_type), used_paths
            )
            link_paths: tuple[PurePosixPath, ...] = ()
            if self.options.organize_by_chat:
                link_paths = (
                    self._unique(
                        self._chat_path(item, media_type, output_path.name), used_paths
                    ),
                )

            planned.append(
                PlannedFile(
                    item=item,
                    output_path=output_path,
                    link_paths=link_paths,
                    media_type=media_type,
                )
            )

        return ExtractionPlan(
            files=tuple(planned),
            excluded=tuple(excluded),
            missing=tuple(missing),
        )

    # -- Zielpfade ----------------------------------------------------------

    def _media_path(self, item: MediaItem, media_type: MediaType) -> PurePosixPath:
        """Pfad in der typbasierten Struktur unter `media/`."""
        filename = self.filename_for(item, media_type)
        if item.is_thumbnail:
            return PurePosixPath(MEDIA_DIR, THUMBNAIL_DIR, filename)
        if self._is_app_internal(media_type):
            return PurePosixPath("metadata", filename)
        if media_type.category is MediaCategory.DATABASE:
            return PurePosixPath("databases", filename)
        return PurePosixPath(MEDIA_DIR, media_type.category.directory, filename)

    def _chat_path(
        self, item: MediaItem, media_type: MediaType, filename: str
    ) -> PurePosixPath:
        """Pfad in der chatbasierten Struktur unter `chats/`."""
        chat = sanitize_component(item.chat.display_name) if item.chat else UNASSIGNED
        sub = THUMBNAIL_DIR if item.is_thumbnail else media_type.category.directory
        return PurePosixPath(CHATS_DIR, chat, sub, filename)

    @staticmethod
    def _is_app_internal(media_type: MediaType) -> bool:
        return (media_type.format_name or "").upper() in APP_INTERNAL_FORMATS

    @staticmethod
    def _unique(
        candidate: PurePosixPath, used: set[PurePosixPath]
    ) -> PurePosixPath:
        """Vermeidet Kollisionen schon im Plan, nicht erst beim Schreiben.

        Sonst wuerde der Probelauf eine andere Dateizahl melden als der echte.
        """
        if candidate not in used:
            used.add(candidate)
            return candidate
        stem = PurePosixPath(candidate.name).stem
        suffix = PurePosixPath(candidate.name).suffix
        for counter in range(1, 10_000):
            alternative = candidate.parent / f"{stem}-{counter}{suffix}"
            if alternative not in used:
                used.add(alternative)
                return alternative
        raise ValueError(f"Zu viele Namenskollisionen fuer {candidate.name}")

    # -- Dateinamen ---------------------------------------------------------

    def filename_for(self, item: MediaItem, media_type: MediaType) -> str:
        """Bestimmt den Ausgabedateinamen.

        Reihenfolge:

        1. Originaldateiname aus der Datenbank, wenn vorhanden.
        2. `YYYY-MM-DD_HH-MM-SS_<typ>_<kennung>.<endung>`
        3. `unknown-date_<kennung>.<endung>`, wenn kein Datum belegbar ist.

        Die Kennung ist die SHA-256-Kurzform der **Quelladresse**, nicht des
        Inhalts. Das ist bewusst so: der Name steht damit schon im Plan fest,
        ohne dass Gigabytes gelesen werden muessten, und `--dry-run` nennt
        genau die Namen, die der echte Lauf schreibt. Der Inhaltshash landet
        trotzdem im Export-Manifest und dient dort der Duplikaterkennung und
        der Integritaetspruefung.
        """
        if item.original_filename:
            name = sanitize_component(item.original_filename)
            if name and name != "unbenannt":
                return self._with_extension(name, media_type)

        identity = hash_bytes(item.source.identity().encode("utf-8"))[:8]
        extension = self._extension(media_type)
        kind = media_type.category.value
        if item.is_thumbnail:
            kind = f"{kind}-thumb"

        if item.timestamp is None:
            return f"unknown-date_{identity}{extension}"
        stamp = self._format_timestamp(item.timestamp)
        return f"{stamp}_{kind}_{identity}{extension}"

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%d_%H-%M-%S")

    @staticmethod
    def _extension(media_type: MediaType) -> str:
        extension = media_type.extension or ""
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        return extension

    def _with_extension(self, name: str, media_type: MediaType) -> str:
        """Ergaenzt eine fehlende Endung, ersetzt aber keine vorhandene.

        Eine vorhandene Endung bleibt stehen, selbst wenn sie dem Inhalt
        widerspricht - der Widerspruch steht im Manifest als
        `extension_mismatch`. Den Originalnamen stillschweigend umzuschreiben
        waere eine Veraenderung der Daten.
        """
        if PurePosixPath(name).suffix:
            return name
        return f"{name}{self._extension(media_type)}"
