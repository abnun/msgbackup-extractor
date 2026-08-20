"""Erzeugt den Index und die HTML-Seite fuer das UI.

Datengrundlage ist ausschliesslich `export-manifest.json`. Das UI liest die
App-Datenbank nicht erneut - deshalb muss alles, was es anzeigen soll, schon im
Manifest stehen.

Aufbau des Index:

* **Eintraege** sind die Originalmedien. Ein Vorschaubild ist kein eigener
  Eintrag, sondern das Feld `preview` seines Originals.
* Vorschaubilder **ohne** Original bleiben eigene Eintraege und werden als
  solche gekennzeichnet. Sie sind haeufig das Einzige, was von einem
  geloeschten Medium uebrig ist - sie wegzulassen waere ein stiller Verlust.
* Feldnamen sind kurz gehalten, weil der Index eingebettet wird: bei [Anzahl entfernt]
  Eintraegen macht das rund ein Drittel der Dateigroesse aus.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Final

from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.extract.export_manifest import MANIFEST_NAME, LoadedManifest
from msgbackup_extractor.models import FileOutcome, SourceKind, TimestampSource

logger = get_logger("ui")

PAGE_NAME: Final = "index.html"
TEMPLATE_NAME: Final = "template.html"

#: Stelle im Template, an der der Index eingesetzt wird.
INDEX_PLACEHOLDER: Final = "/*__INDEX__*/"

#: Grobe Medienklasse fuer die Filterleiste.
_KIND_BY_TOP_LEVEL: Final[dict[str, str]] = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "text": "document",
    "application": "document",
}

#: Verzeichnisse, deren Inhalt keine Nutzmedien sind: App-Einstellungen, Logs,
#: die App-Datenbanken selbst. Sie bekommen eine eigene Klasse, damit sie die
#: Galerie nicht dominieren - ausgeblendet werden sie nicht.
_INTERNAL_PREFIXES: Final = ("metadata/", "databases/")


class UiBuildError(RuntimeError):
    """Das UI kann nicht erzeugt werden."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """Ein Eintrag im UI-Index."""

    path: str
    kind: str
    mime: str | None
    size: int | None
    chat: int | None
    timestamp: int | None
    name: str | None
    preview: str | None
    #: True, wenn nur ein Vorschaubild vorliegt und das Original fehlt.
    preview_only: bool
    #: True, wenn Inhalt und Dateiendung sich widersprechen.
    mismatch: bool
    #: Index des Messengers in `messengers`. None bei einer Einzelseite.
    messenger: int | None = None
    #: Zeitstempel der Backupdatei. Bewusst getrennt von `timestamp`: er sagt,
    #: wann das Backup die Datei schrieb, nicht wann der Inhalt entstand. Auf
    #: einer Zeitachse waere er irrefuehrend, als Angabe im Detail nuetzlich.
    file_time: int | None = None

    def to_json(self) -> dict[str, Any]:
        """Kurze Schluessel, weil der Index in die Seite eingebettet wird."""
        data: dict[str, Any] = {"p": self.path, "k": self.kind}
        if self.mime:
            data["m"] = self.mime
        if self.size is not None:
            data["s"] = self.size
        if self.chat is not None:
            data["c"] = self.chat
        if self.timestamp is not None:
            data["t"] = self.timestamp
        if self.file_time is not None:
            data["f"] = self.file_time
        if self.name:
            data["n"] = self.name
        if self.preview:
            data["v"] = self.preview
        if self.preview_only:
            data["o"] = 1
        if self.mismatch:
            data["x"] = 1
        if self.messenger is not None:
            data["g"] = self.messenger
        return data


def _identity(entry: Any) -> str | None:
    """Kennung der Quelle, wie sie `MediaSource.identity()` bildet."""
    if entry.get("source_kind") == SourceKind.EXTERNAL_FILE.value:
        file_id = entry.get("source_file_id")
        return f"file:{file_id}" if file_id else None
    table = entry.get("source_table")
    row = entry.get("source_row_id")
    if table is None or row is None:
        return None
    return f"blob:{table}:{row}:ZDATA"


def _kind(mime: str | None, path: str) -> str:
    if path.startswith(_INTERNAL_PREFIXES):
        return "internal"
    if not mime:
        return "other"
    return _KIND_BY_TOP_LEVEL.get(mime.split("/", 1)[0], "other")


def _split_time(entry: dict[str, Any]) -> tuple[int | None, int | None]:
    """Trennt Nachrichtendatum von Dateidatum.

    Nur ein Nachrichtendatum taugt fuer die Zeitachse. Ein Dateidatum stammt
    aus den Backup-Metadaten und liegt fuer alle betroffenen Dateien am Tag des
    Backups - auf einer Zeitachse wuerde es einen Klumpen bilden, der nichts
    aussagt.
    """
    stamp = _unix_seconds(entry.get("timestamp"))
    if stamp is None:
        return None, None
    if entry.get("timestamp_source") == TimestampSource.FILE.value:
        return None, stamp
    return stamp, None


def _unix_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp())


def build_index(
    manifest: LoadedManifest,
    *,
    raw: dict[str, Any],
    path_prefix: str = "",
    messenger: int | None = None,
) -> dict[str, Any]:
    """Baut den Index fuer die Seite aus dem geladenen Manifest.

    Args:
        manifest: Das geladene Manifest, fuer Kopfdaten.
        raw: Die rohe Manifest-Struktur - sie enthaelt Felder, die
            `LoadedManifest` bewusst nicht mitfuehrt (Chat, Zeitstempel,
            Originalname), weil `verify` sie nicht braucht.
        path_prefix: Wird jedem Pfad vorangestellt. Fuer eine gemeinsame Seite
            mehrerer Messenger, deren Exporte in Unterverzeichnissen liegen.
        messenger: Index des Messengers, wenn mehrere auf einer Seite sind.

    Raises:
        UiBuildError: Wenn das Manifest aus einem Probelauf stammt oder keine
            verwertbaren Eintraege hat.
    """
    if manifest.dry_run:
        raise UiBuildError(
            "Dieses Manifest stammt aus einem Probelauf. Es verweist auf keine "
            "Dateien, deshalb gibt es nichts anzuzeigen."
        )

    files = [
        entry
        for entry in raw.get("files", [])
        if entry.get("outcome")
        in (FileOutcome.EXTRACTED.value, FileOutcome.DUPLICATE.value)
        and entry.get("output_path")
    ]
    if not files:
        raise UiBuildError(
            "Das Manifest enthaelt keine exportierten Dateien. Wurde der Export "
            "abgebrochen?"
        )

    originals = [entry for entry in files if not entry.get("is_thumbnail")]
    thumbnails = [entry for entry in files if entry.get("is_thumbnail")]

    #: Kennung des Originals -> Pfad seines Vorschaubilds.
    preview_by_original: dict[str, str] = {}
    for thumbnail in thumbnails:
        target = thumbnail.get("thumbnail_of")
        if target:
            preview_by_original.setdefault(
                target, path_prefix + thumbnail["output_path"]
            )

    used_previews = {p.removeprefix(path_prefix) for p in preview_by_original.values()}

    # Chatnamen sammeln und nach Haeufigkeit ordnen, damit die Filterleiste die
    # grossen Chats zuerst zeigt.
    counts: Counter[str] = Counter(
        entry["chat_name"] for entry in files if entry.get("chat_name")
    )
    chats = [name for name, _ in counts.most_common()]
    chat_index = {name: position for position, name in enumerate(chats)}

    entries: list[IndexEntry] = []

    for entry in originals:
        identity = _identity(entry)
        preview = preview_by_original.get(identity) if identity else None
        mime = entry.get("media_type")
        kind = _kind(mime, entry["output_path"])
        stamp, file_time = _split_time(entry)
        if preview is None and kind == "image":
            # Ohne eigenes Vorschaubild dient das Original als Kachel.
            preview = path_prefix + entry["output_path"]
        entries.append(
            IndexEntry(
                path=path_prefix + entry["output_path"],
                kind=kind,
                mime=mime,
                size=entry.get("size"),
                chat=chat_index.get(entry.get("chat_name")),
                timestamp=stamp,
                file_time=file_time,
                name=entry.get("original_filename"),
                preview=preview,
                preview_only=False,
                mismatch=bool(entry.get("extension_mismatch")),
                messenger=messenger,
            )
        )

    # Vorschaubilder ohne Original: eigener Eintrag, gekennzeichnet.
    for thumbnail in thumbnails:
        if thumbnail["output_path"] in used_previews:
            continue
        stamp, file_time = _split_time(thumbnail)
        entries.append(
            IndexEntry(
                path=path_prefix + thumbnail["output_path"],
                kind=_kind(thumbnail.get("media_type"), thumbnail["output_path"]),
                mime=thumbnail.get("media_type"),
                size=thumbnail.get("size"),
                chat=chat_index.get(thumbnail.get("chat_name")),
                timestamp=stamp,
                file_time=file_time,
                name=thumbnail.get("original_filename"),
                preview=path_prefix + thumbnail["output_path"],
                preview_only=True,
                mismatch=bool(thumbnail.get("extension_mismatch")),
                messenger=messenger,
            )
        )

    # Neueste zuerst; Eintraege ohne Datum ans Ende, damit sie nicht die
    # Zeitachse verwirren - sichtbar bleiben sie trotzdem.
    entries.sort(key=lambda e: (e.timestamp is None, -(e.timestamp or 0), e.path))

    summary = raw.get("summary", {})
    return {
        "app": manifest.app,
        "udid": manifest.backup_udid,
        "generated_at": manifest.generated_at,
        "tool_version": raw.get("tool_version"),
        "chats": chats,
        "counts": {
            "entries": len(entries),
            "originals": len(originals),
            "thumbnails": len(thumbnails),
            "paired": len(preview_by_original),
            "preview_only": sum(1 for e in entries if e.preview_only),
            "without_chat": sum(1 for e in entries if e.chat is None),
            "without_date": sum(1 for e in entries if e.timestamp is None),
            "internal": sum(1 for e in entries if e.kind == "internal"),
            "file_time_only": sum(
                1 for e in entries if e.timestamp is None and e.file_time is not None
            ),
            "mismatch": sum(1 for e in entries if e.mismatch),
            "total_bytes": summary.get("total_bytes"),
            "item_bytes": sum(e.size or 0 for e in entries),
            "manifest_total": summary.get("total"),
        },
        "items": [entry.to_json() for entry in entries],
    }


#: Kennzeichen, an dem eine von diesem Programm erzeugte Seite erkennbar ist.
GENERATOR_MARKER: Final = '<meta name="generator" content="msgbackup-extractor">'


def is_generated_page(path: Path) -> bool:
    """Wurde diese Seite von diesem Programm erzeugt?

    Wird gebraucht, bevor eine bestehende `index.html` ueberschrieben wird. Eine
    fremde Datei gleichen Namens zu ersetzen waere Datenverlust - der Kopf der
    Datei genuegt fuer die Entscheidung.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return GENERATOR_MARKER in handle.read(4096)
    except OSError:
        return False


def refresh_pages(export_dir: Path, loader: Any, raw_loader: Any) -> list[Path]:
    """Erzeugt die Seite dieses Exports neu, und die Uebersicht falls vorhanden.

    Rueckgabe sind die geschriebenen Pfade.

    Die Uebersicht liegt im **Elternverzeichnis** des Exports und damit
    ausserhalb von `--output`. Sie wird deshalb nur aktualisiert, wenn dort
    schon eine von diesem Programm erzeugte Seite liegt: dann hat der Nutzer
    dieses Verzeichnis bereits als Uebersichtsort bestimmt, und eine
    Aktualisierung ist erwartet statt ueberraschend. Eine neue Datei ausserhalb
    von `--output` anzulegen waere ein Bruch der Zusage.
    """
    written: list[Path] = []

    manifest_path = export_dir / MANIFEST_NAME
    index = build_index(loader(manifest_path), raw=raw_loader(manifest_path))
    written.append(write_page(index, export_dir))

    overview = export_dir.parent / PAGE_NAME
    if overview.parent == export_dir or not is_generated_page(overview):
        return written

    exports = discover_exports(export_dir.parent)
    if len(exports) < 2:
        return written
    combined = build_combined_index(exports, loader, raw_loader)
    written.append(write_page(combined, export_dir.parent))
    return written


def _template() -> str:
    """Liest das HTML-Grundgeruest aus den Paketdaten."""
    try:
        return (
            resources.files("msgbackup_extractor.ui")
            .joinpath(TEMPLATE_NAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as error:  # pragma: no cover
        raise UiBuildError(
            f"Die UI-Vorlage {TEMPLATE_NAME} fehlt in der Installation."
        ) from error


def render_page(index: dict[str, Any]) -> str:
    """Setzt den Index in die Vorlage ein.

    Der Index wird als JSON eingebettet. `</script>` im Inhalt wuerde die Seite
    zerreissen, deshalb wird der Schraegstrich maskiert - das ist im JSON
    zulaessig und der Parser liest es unveraendert.
    """
    template = _template()
    if INDEX_PLACEHOLDER not in template:
        raise UiBuildError(  # pragma: no cover - Vorlage ist Paketdatei
            "Die UI-Vorlage enthaelt keine Einsetzstelle fuer den Index."
        )
    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    return template.replace(INDEX_PLACEHOLDER, payload)


def write_page(index: dict[str, Any], output_dir: Path) -> Path:
    """Schreibt `index.html` in das Ausgabeverzeichnis."""
    target = output_dir / PAGE_NAME
    target.write_text(render_page(index), encoding="utf-8")
    logger.debug("UI geschrieben: %d Eintraege", len(index.get("items", ())))
    return target


def load_raw_manifest(path: Path) -> dict[str, Any]:
    """Liest die rohe Manifest-Struktur."""
    if path.is_dir():
        path = path / MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UiBuildError(f"Es gibt keine Datei {path.name}.") from error
    except json.JSONDecodeError as error:
        raise UiBuildError(f"{path.name} ist kein gueltiges JSON: {error}") from error


# ---------------------------------------------------------------------------
# Mehrere Messenger auf einer Seite
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportRef:
    """Ein gefundener Export unterhalb eines gemeinsamen Verzeichnisses."""

    #: Verzeichnisname, gleichzeitig das Pfadpraefix.
    directory: str
    manifest_path: Path
    app: str | None

    @property
    def label(self) -> str:
        """Anzeigename des Messengers.

        Der Name kommt aus dem registrierten Profil, damit die Schreibweise
        stimmt - aus dem Slug "whatsapp" wuerde sonst "Whatsapp".
        """
        from msgbackup_extractor.apps.registry import get_profile

        slug = self.app or self.directory
        try:
            return get_profile(slug).name
        except KeyError:
            return slug[:1].upper() + slug[1:]


def discover_exports(root: Path) -> list[ExportRef]:
    """Findet Exporte unterhalb eines Verzeichnisses.

    Ein Export ist ein Verzeichnis mit `export-manifest.json`. Gesucht wird nur
    eine Ebene tief - tiefer zu suchen wuerde bei einem falsch gewaehlten
    Wurzelverzeichnis lange dauern und Fremdes einsammeln.
    """
    found: list[ExportRef] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / MANIFEST_NAME
        if not manifest.is_file():
            continue
        app: str | None = None
        with suppress(OSError, json.JSONDecodeError):
            app = json.loads(manifest.read_text(encoding="utf-8")).get("app")
        found.append(ExportRef(directory=child.name, manifest_path=manifest, app=app))
    return found


def build_combined_index(
    exports: list[ExportRef], loader: Any, raw_loader: Any
) -> dict[str, Any]:
    """Fuehrt mehrere Exporte zu einem Index zusammen.

    Chatnamen koennen sich zwischen Messengern wiederholen, deshalb traegt jeder
    Chat seinen Messenger mit - sonst wuerden zwei verschiedene Chats zu einem
    verschmelzen und die Zaehlung waere falsch.

    Args:
        loader: Funktion, die aus einem Pfad ein `LoadedManifest` macht.
        raw_loader: Funktion, die aus einem Pfad die rohe Struktur macht.
    """
    if not exports:
        raise UiBuildError(
            "Unterhalb dieses Verzeichnisses wurde kein Export gefunden. "
            f"Ein Export ist ein Verzeichnis mit einer {MANIFEST_NAME}."
        )

    messengers = [ref.label for ref in exports]
    chats: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    per_messenger: list[dict[str, Any]] = []
    generated: list[str] = []

    for position, ref in enumerate(exports):
        manifest = loader(ref.manifest_path)
        index = build_index(
            manifest,
            raw=raw_loader(ref.manifest_path),
            path_prefix=f"{ref.directory}/",
            messenger=position,
        )

        # Lokale Chatindizes auf die gemeinsame Liste umschreiben.
        offset = len(chats)
        chats.extend({"n": name, "g": position} for name in index["chats"])
        for item in index["items"]:
            if "c" in item:
                item["c"] += offset
            items.append(item)

        for key, value in index["counts"].items():
            if isinstance(value, int):
                counts[key] = counts.get(key, 0) + value
        per_messenger.append(
            {
                "label": ref.label,
                "app": index["app"],
                "directory": ref.directory,
                "entries": index["counts"]["entries"],
                "generated_at": index["generated_at"],
            }
        )
        if index["generated_at"]:
            generated.append(index["generated_at"])

    # Global neu sortieren: neueste zuerst, Undatiertes ans Ende.
    items.sort(key=lambda i: (("t" not in i), -i.get("t", 0), i["p"]))

    counts["messengers"] = len(messengers)
    return {
        "app": None,
        "udid": None,
        "generated_at": max(generated) if generated else None,
        "tool_version": None,
        "messengers": messengers,
        "sources": per_messenger,
        "chats": [c["n"] for c in chats],
        "chat_messengers": [c["g"] for c in chats],
        "counts": counts,
        "items": items,
    }
