"""Medienerkennung anhand von Dateisignaturen.

Die Reihenfolge ist bewusst: **Magic Bytes** vor **MIME** vor **Dateiendung**.
Dateien in iOS-Backups tragen ihre Endung im `relativePath`, aber Messenger
speichern Anhaenge haeufig mit generischen Namen oder falschen Endungen. Ein
Widerspruch zwischen Inhalt und Endung wird deshalb festgehalten
(`extension_mismatch`), nicht stillschweigend aufgeloest.

Alles offline: es werden nur Bytes verglichen.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from msgbackup_extractor.models import DetectionMethod, MediaCategory, MediaType

#: So viele Bytes reichen fuer alle hier verwendeten Signaturen.
HEADER_SIZE: Final = 4096

UNKNOWN = MediaType(
    category=MediaCategory.OTHER,
    mime_type=None,
    extension=None,
    detection_method=DetectionMethod.UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class Signature:
    """Eine Dateisignatur."""

    format_name: str
    category: MediaCategory
    mime_type: str
    extension: str
    #: Bytes, die an `offset` stehen muessen.
    prefix: bytes
    offset: int = 0
    #: Zusaetzliche Bytes, die an `extra_offset` stehen muessen (z.B. RIFF/WEBP).
    extra: bytes | None = None
    extra_offset: int = 0

    def matches(self, header: bytes) -> bool:
        if header[self.offset : self.offset + len(self.prefix)] != self.prefix:
            return False
        if self.extra is None:
            return True
        return header[self.extra_offset : self.extra_offset + len(self.extra)] == self.extra


#: `ftyp`-Brands: bei ISO-BMFF steht der Typ ab Offset 8.
_FTYP_BRANDS: Final[dict[bytes, tuple[str, MediaCategory, str, str]]] = {
    # Bilder
    b"heic": ("HEIC", MediaCategory.IMAGE, "image/heic", ".heic"),
    b"heix": ("HEIC", MediaCategory.IMAGE, "image/heic", ".heic"),
    b"heim": ("HEIC", MediaCategory.IMAGE, "image/heic", ".heic"),
    b"heis": ("HEIC", MediaCategory.IMAGE, "image/heic", ".heic"),
    b"hevc": ("HEIC", MediaCategory.IMAGE, "image/heic-sequence", ".heic"),
    b"hevm": ("HEIC", MediaCategory.IMAGE, "image/heic-sequence", ".heic"),
    b"hevs": ("HEIC", MediaCategory.IMAGE, "image/heic-sequence", ".heic"),
    b"mif1": ("HEIF", MediaCategory.IMAGE, "image/heif", ".heif"),
    b"msf1": ("HEIF", MediaCategory.IMAGE, "image/heif-sequence", ".heif"),
    b"avif": ("AVIF", MediaCategory.IMAGE, "image/avif", ".avif"),
    # Video
    b"qt  ": ("MOV", MediaCategory.VIDEO, "video/quicktime", ".mov"),
    b"M4V ": ("M4V", MediaCategory.VIDEO, "video/x-m4v", ".m4v"),
    b"M4VH": ("M4V", MediaCategory.VIDEO, "video/x-m4v", ".m4v"),
    b"M4VP": ("M4V", MediaCategory.VIDEO, "video/x-m4v", ".m4v"),
    b"isom": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"iso2": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"iso4": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"mp41": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"mp42": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"avc1": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"dash": ("MP4", MediaCategory.VIDEO, "video/mp4", ".mp4"),
    b"3gp4": ("3GP", MediaCategory.VIDEO, "video/3gpp", ".3gp"),
    b"3gp5": ("3GP", MediaCategory.VIDEO, "video/3gpp", ".3gp"),
    # Audio
    b"M4A ": ("M4A", MediaCategory.AUDIO, "audio/mp4", ".m4a"),
    b"M4B ": ("M4B", MediaCategory.AUDIO, "audio/mp4", ".m4b"),
    b"mp4a": ("M4A", MediaCategory.AUDIO, "audio/mp4", ".m4a"),
    b"F4A ": ("F4A", MediaCategory.AUDIO, "audio/mp4", ".f4a"),
}

#: Reihenfolge ist relevant: spezifischere Signaturen zuerst.
SIGNATURES: Final[tuple[Signature, ...]] = (
    # -- Datenbanken (vor allem anderen, damit .sqlite nicht als Text gilt) --
    Signature("SQLite", MediaCategory.DATABASE, "application/vnd.sqlite3", ".sqlite",
              b"SQLite format 3\x00"),
    # -- Bilder ------------------------------------------------------------
    Signature("JPEG", MediaCategory.IMAGE, "image/jpeg", ".jpg", b"\xff\xd8\xff"),
    Signature("PNG", MediaCategory.IMAGE, "image/png", ".png", b"\x89PNG\r\n\x1a\n"),
    Signature("GIF", MediaCategory.IMAGE, "image/gif", ".gif", b"GIF89a"),
    Signature("GIF", MediaCategory.IMAGE, "image/gif", ".gif", b"GIF87a"),
    Signature("WEBP", MediaCategory.IMAGE, "image/webp", ".webp", b"RIFF",
              extra=b"WEBP", extra_offset=8),
    Signature("BMP", MediaCategory.IMAGE, "image/bmp", ".bmp", b"BM"),
    Signature("TIFF", MediaCategory.IMAGE, "image/tiff", ".tiff", b"II*\x00"),
    Signature("TIFF", MediaCategory.IMAGE, "image/tiff", ".tiff", b"MM\x00*"),
    # -- Audio -------------------------------------------------------------
    Signature("WAV", MediaCategory.AUDIO, "audio/wav", ".wav", b"RIFF",
              extra=b"WAVE", extra_offset=8),
    Signature("CAF", MediaCategory.AUDIO, "audio/x-caf", ".caf", b"caff"),
    Signature("AMR", MediaCategory.AUDIO, "audio/amr", ".amr", b"#!AMR"),
    Signature("FLAC", MediaCategory.AUDIO, "audio/flac", ".flac", b"fLaC"),
    Signature("MP3", MediaCategory.AUDIO, "audio/mpeg", ".mp3", b"ID3"),
    Signature("AAC", MediaCategory.AUDIO, "audio/aac", ".aac", b"\xff\xf1"),
    Signature("AAC", MediaCategory.AUDIO, "audio/aac", ".aac", b"\xff\xf9"),
    # -- Dokumente ---------------------------------------------------------
    Signature("PDF", MediaCategory.DOCUMENT, "application/pdf", ".pdf", b"%PDF-"),
    Signature("RTF", MediaCategory.DOCUMENT, "application/rtf", ".rtf", b"{\\rtf"),
    Signature("VCARD", MediaCategory.DOCUMENT, "text/vcard", ".vcf", b"BEGIN:VCARD"),
    Signature("ICAL", MediaCategory.DOCUMENT, "text/calendar", ".ics", b"BEGIN:VCALENDAR"),
    Signature("PLIST", MediaCategory.DOCUMENT, "application/x-plist", ".plist", b"bplist00"),
    # -- Archive / Office --------------------------------------------------
    Signature("OLE2", MediaCategory.DOCUMENT, "application/x-ole-storage", ".doc",
              b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    Signature("GZIP", MediaCategory.ARCHIVE, "application/gzip", ".gz", b"\x1f\x8b"),
    Signature("7Z", MediaCategory.ARCHIVE, "application/x-7z-compressed", ".7z",
              b"7z\xbc\xaf\x27\x1c"),
    Signature("RAR", MediaCategory.ARCHIVE, "application/vnd.rar", ".rar", b"Rar!\x1a\x07"),
)

#: OOXML- und iWork-Container liegen alle als ZIP vor; unterschieden wird ueber
#: die Namen der enthaltenen Eintraege im Kopfbereich der Datei.
_ZIP_MARKERS: Final[tuple[tuple[bytes, str, MediaCategory, str, str], ...]] = (
    (b"word/", "DOCX", MediaCategory.DOCUMENT,
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    (b"xl/", "XLSX", MediaCategory.DOCUMENT,
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    (b"ppt/", "PPTX", MediaCategory.DOCUMENT,
     "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    (b"Pages", "PAGES", MediaCategory.DOCUMENT, "application/x-iwork-pages-sffpages", ".pages"),
    (b"Numbers", "NUMBERS", MediaCategory.DOCUMENT,
     "application/x-iwork-numbers-sffnumbers", ".numbers"),
    (b"Keynote", "KEYNOTE", MediaCategory.DOCUMENT,
     "application/x-iwork-keynote-sffkey", ".key"),
)

#: OLE2-Container: DOC, XLS und PPT sind ohne Tiefeninspektion nicht sicher zu
#: trennen. Die Endung entscheidet dann - und `extension_mismatch` bleibt False,
#: weil kein Widerspruch besteht, nur eine Unschaerfe.
_OLE_BY_EXTENSION: Final[dict[str, tuple[str, str]]] = {
    ".doc": ("DOC", "application/msword"),
    ".xls": ("XLS", "application/vnd.ms-excel"),
    ".ppt": ("PPT", "application/vnd.ms-powerpoint"),
}

#: Endungen, die als Text gelten, wenn keine Signatur greift.
_TEXT_EXTENSIONS: Final = frozenset({".txt", ".text", ".log", ".md", ".csv", ".json", ".xml"})

#: Endungen, die zu derselben Kategorie gehoeren und daher keinen Mismatch
#: darstellen (z.B. `.jpeg` gegen erkanntes `.jpg`).
_EQUIVALENT_EXTENSIONS: Final[tuple[frozenset[str], ...]] = (
    frozenset({".jpg", ".jpeg", ".jpe"}),
    frozenset({".tif", ".tiff"}),
    frozenset({".heic", ".heif", ".hif"}),
    frozenset({".mp4", ".m4v", ".mov"}),
    frozenset({".m4a", ".m4b", ".aac", ".mp4"}),
    frozenset({".sqlite", ".sqlite3", ".db"}),
    frozenset({".opus", ".ogg", ".oga", ".ogv"}),
    frozenset({".yml", ".yaml"}),
)


def _extensions_are_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    return any({left, right} <= group for group in _EQUIVALENT_EXTENSIONS)


def _is_probably_text(header: bytes) -> bool:
    """Heuristik: dekodierbar als UTF-8 und ohne Steuerzeichen ausser Whitespace."""
    if not header:
        return False
    if b"\x00" in header:
        return False
    try:
        text = header.decode("utf-8")
    except UnicodeDecodeError:
        return False
    allowed_control = {"\n", "\r", "\t", "\f", "\v"}
    return all(character >= " " or character in allowed_control for character in text)


def _match_ftyp(header: bytes) -> tuple[str, MediaCategory, str, str] | None:
    """Erkennt ISO-BMFF-Container ueber die `ftyp`-Box."""
    if header[4:8] != b"ftyp":
        return None
    brand = header[8:12]
    if (found := _FTYP_BRANDS.get(brand)) is not None:
        return found
    # Unbekannter Brand: es ist nachweislich ISO-BMFF, aber der Untertyp ist
    # offen. Als Video einzuordnen waere geraten - deshalb OTHER mit Vermerk.
    return (
        f"ISO-BMFF ({brand.decode('ascii', 'replace').strip()})",
        MediaCategory.OTHER,
        "application/octet-stream",
        "",
    )


def _match_ogg(header: bytes) -> tuple[str, MediaCategory, str, str] | None:
    """Erkennt Ogg-Container und deren Untertyp.

    "OggS" allein sagt nur, dass es ein Ogg-Container ist. Der Codec steht im
    Kopf der ersten Page: `OpusHead` fuer Opus, `\x01vorbis` fuer Vorbis,
    `\x7fFLAC` fuer FLAC-in-Ogg. Ohne Treffer bleibt es beim generischen Ogg.
    """
    if not header.startswith(b"OggS"):
        return None
    if b"OpusHead" in header:
        return ("OPUS", MediaCategory.AUDIO, "audio/opus", ".opus")
    if b"\x01vorbis" in header:
        return ("OGG", MediaCategory.AUDIO, "audio/vorbis", ".ogg")
    if b"\x7fFLAC" in header:
        return ("OGG-FLAC", MediaCategory.AUDIO, "audio/ogg", ".oga")
    if b"\x80theora" in header:
        return ("OGV", MediaCategory.VIDEO, "video/ogg", ".ogv")
    return ("OGG", MediaCategory.AUDIO, "audio/ogg", ".ogg")


def _match_zip(header: bytes) -> tuple[str, MediaCategory, str, str] | None:
    """Unterscheidet OOXML/iWork von einem gewoehnlichen ZIP-Archiv."""
    if not header.startswith(b"PK\x03\x04"):
        return None
    for marker, name, category, mime, extension in _ZIP_MARKERS:
        if marker in header:
            return (name, category, mime, extension)
    return ("ZIP", MediaCategory.ARCHIVE, "application/zip", ".zip")


def detect(header: bytes, *, filename: str | None = None) -> MediaType:
    """Bestimmt den Medientyp aus Signatur, MIME-Datenbank und Endung.

    Args:
        header: Die ersten Bytes des Inhalts (mindestens 32, ideal `HEADER_SIZE`).
        filename: Dateiname oder `relativePath`, nur fuer Endung und
            Mismatch-Erkennung. Beeinflusst die Signaturerkennung nicht.
    """
    declared_extension = PurePosixPath(filename).suffix.lower() if filename else ""

    found = _match_ftyp(header) or _match_ogg(header) or _match_zip(header)
    if found is None:
        for signature in SIGNATURES:
            if signature.matches(header):
                found = (
                    signature.format_name,
                    signature.category,
                    signature.mime_type,
                    signature.extension,
                )
                break

    if found is not None:
        format_name, category, mime_type, extension = found

        # OLE2 laesst sich nur ueber die Endung weiter aufloesen.
        if format_name == "OLE2" and declared_extension in _OLE_BY_EXTENSION:
            format_name, mime_type = _OLE_BY_EXTENSION[declared_extension]
            extension = declared_extension

        mismatch = bool(
            declared_extension
            and extension
            and not _extensions_are_equivalent(declared_extension, extension)
        )
        return MediaType(
            category=category,
            mime_type=mime_type,
            extension=extension or declared_extension or None,
            detection_method=DetectionMethod.MAGIC,
            extension_mismatch=mismatch,
            format_name=format_name,
        )

    # Keine Signatur: MIME-Datenbank ueber die Endung.
    if declared_extension:
        guessed, _ = mimetypes.guess_type(f"x{declared_extension}")
        if guessed is not None:
            return MediaType(
                category=_category_from_mime(guessed),
                mime_type=guessed,
                extension=declared_extension,
                detection_method=DetectionMethod.MIME,
                format_name=declared_extension.lstrip(".").upper(),
            )

    if _is_probably_text(header):
        return MediaType(
            category=MediaCategory.DOCUMENT,
            mime_type="text/plain",
            extension=declared_extension or ".txt",
            detection_method=(
                DetectionMethod.EXTENSION if declared_extension in _TEXT_EXTENSIONS
                else DetectionMethod.MAGIC
            ),
            format_name="TEXT",
        )

    if declared_extension:
        return MediaType(
            category=MediaCategory.OTHER,
            mime_type=None,
            extension=declared_extension,
            detection_method=DetectionMethod.EXTENSION,
            format_name=declared_extension.lstrip(".").upper(),
        )

    return UNKNOWN


def _category_from_mime(mime_type: str) -> MediaCategory:
    top_level = mime_type.split("/", 1)[0]
    return {
        "image": MediaCategory.IMAGE,
        "video": MediaCategory.VIDEO,
        "audio": MediaCategory.AUDIO,
        "text": MediaCategory.DOCUMENT,
    }.get(top_level, MediaCategory.OTHER)


def detect_file(path: Path, *, filename: str | None = None) -> MediaType:
    """Wie `detect()`, liest den Kopf der Datei selbst. Oeffnet nur lesend."""
    with path.open("rb") as handle:
        header = handle.read(HEADER_SIZE)
    return detect(header, filename=filename or path.name)


# ---------------------------------------------------------------------------
# Pixelmasse
# ---------------------------------------------------------------------------

#: Bis hierhin wird nach den Massangaben gesucht. JPEG-Dateien tragen vor dem
#: SOF-Marker oft einen EXIF-Block mit eingebettetem Vorschaubild von einigen
#: zehn Kilobyte; 4 KB reichen dafuer nicht.
DIMENSION_SCAN_LIMIT: Final = 1024 * 1024

#: JPEG-Marker, die eine Bildgroesse tragen (SOF0-SOF15 ohne DHT/DAC/RST).
_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Liest Breite und Hoehe aus den JPEG-Segmenten.

    Die Groesse steht im SOF-Segment, und davor koennen beliebig viele andere
    Segmente liegen. Deshalb wird die Segmentkette entlanggegangen statt an
    einer festen Stelle gelesen.
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    total = len(data)
    while offset + 4 <= total:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # Bildende bzw. Bilddaten
            return None
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            if offset + 9 > total:
                return None
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return (width, height) if width and height else None
        offset += 2 + length
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Liest Breite und Hoehe aus dem IHDR-Block."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or not data.startswith((b"GIF87a", b"GIF89a")):
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return (width, height) if width and height else None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """WEBP kennt drei Varianten: VP8, VP8L und VP8X."""
    if len(data) < 30 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return (width, height)
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixelmasse eines Bildes, aus dem Dateianfang gelesen.

    Warum das hier steht und nicht in einer Bildbibliothek: es ist
    Formatparsing wie das Keybag-TLV und das NSKeyedArchiver-Plist, und eine
    Bildbibliothek waere eine zweite Laufzeitabhaengigkeit fuer vier
    Ganzzahlen.

    Warum die Masse gebraucht werden: die von den Messengern gespeicherten
    Vorschaubilder sind teils winzig - bei WhatsApp im Median 100 x 73 Pixel.
    In einer Galeriekachel auf einem Retina-Display waere das eine vierfache
    Hochskalierung. Mit den echten Massen kann die Ansicht je Kachel die
    passende Auflaesung waehlen, statt zu raten.

    Gibt None zurueck, wenn das Format keine Masse hergibt oder die Angabe
    nicht im uebergebenen Anfang liegt. Nichts wird geschaetzt.
    """
    for reader in (_jpeg_dimensions, _png_dimensions, _gif_dimensions, _webp_dimensions):
        found = reader(data)
        if found is not None:
            return found
    return None
