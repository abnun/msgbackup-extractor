"""Tests fuer die Medienerkennung."""

from __future__ import annotations

from pathlib import Path

import pytest

from msgbackup_extractor.core import media
from msgbackup_extractor.models import DetectionMethod, MediaCategory

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
OPUS = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead\x01\x02"
DOCX = b"PK\x03\x04\x14\x00\x00\x00\x08\x00[Content_Types].xmlword/document.xml"


@pytest.mark.parametrize(
    ("header", "filename", "format_name", "category"),
    [
        (JPEG, "a.jpg", "JPEG", MediaCategory.IMAGE),
        (PNG, "a.png", "PNG", MediaCategory.IMAGE),
        (b"GIF89a\x00\x00", "a.gif", "GIF", MediaCategory.IMAGE),
        (b"GIF87a\x00\x00", "a.gif", "GIF", MediaCategory.IMAGE),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "a.webp", "WEBP", MediaCategory.IMAGE),
        (b"\x00\x00\x00\x18ftypheic", "a.heic", "HEIC", MediaCategory.IMAGE),
        (b"\x00\x00\x00\x18ftypmif1", "a.heif", "HEIF", MediaCategory.IMAGE),
        (b"\x00\x00\x00\x18ftypmp42", "a.mp4", "MP4", MediaCategory.VIDEO),
        (b"\x00\x00\x00\x18ftypisom", "a.mp4", "MP4", MediaCategory.VIDEO),
        (b"\x00\x00\x00\x18ftypqt  ", "a.mov", "MOV", MediaCategory.VIDEO),
        (b"\x00\x00\x00\x18ftypM4V ", "a.m4v", "M4V", MediaCategory.VIDEO),
        (b"\x00\x00\x00\x20ftypM4A ", "a.m4a", "M4A", MediaCategory.AUDIO),
        (b"\xff\xf1\x50\x80", "a.aac", "AAC", MediaCategory.AUDIO),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "a.wav", "WAV", MediaCategory.AUDIO),
        (OPUS, "a.opus", "OPUS", MediaCategory.AUDIO),
        (b"OggS\x00\x02" + b"\x00" * 20 + b"\x01vorbis", "a.ogg", "OGG", MediaCategory.AUDIO),
        (b"caff\x00\x01", "a.caf", "CAF", MediaCategory.AUDIO),
        (b"%PDF-1.7\n", "a.pdf", "PDF", MediaCategory.DOCUMENT),
        (b"{\\rtf1\\ansi", "a.rtf", "RTF", MediaCategory.DOCUMENT),
        (DOCX, "a.docx", "DOCX", MediaCategory.DOCUMENT),
        (b"PK\x03\x04\x14\x00\x00\x00\x08\x00xl/workbook.xml", "a.xlsx", "XLSX",
         MediaCategory.DOCUMENT),
        (b"PK\x03\x04\x14\x00\x00\x00\x08\x00ppt/presentation.xml", "a.pptx", "PPTX",
         MediaCategory.DOCUMENT),
        (b"PK\x03\x04\x14\x00\x00\x00\x08\x00irgendwas", "a.zip", "ZIP", MediaCategory.ARCHIVE),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a.doc", "DOC", MediaCategory.DOCUMENT),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a.xls", "XLS", MediaCategory.DOCUMENT),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a.ppt", "PPT", MediaCategory.DOCUMENT),
        (b"SQLite format 3\x00", "a.sqlite", "SQLite", MediaCategory.DATABASE),
        (b"BEGIN:VCARD\r\nVERSION", "a.vcf", "VCARD", MediaCategory.DOCUMENT),
        (b"bplist00\xd1", "a.plist", "PLIST", MediaCategory.DOCUMENT),
    ],
)
def test_signatures_are_detected_from_magic_bytes(
    header: bytes, filename: str, format_name: str, category: MediaCategory
) -> None:
    result = media.detect(header, filename=filename)
    assert result.format_name == format_name
    assert result.category is category
    assert result.detection_method is DetectionMethod.MAGIC
    assert not result.extension_mismatch


def test_content_wins_over_extension() -> None:
    """Eine falsche Endung darf die Einordnung nicht bestimmen."""
    result = media.detect(PNG, filename="getarnt.txt")
    assert result.category is MediaCategory.IMAGE
    assert result.format_name == "PNG"
    assert result.extension_mismatch


@pytest.mark.parametrize(
    ("filename", "expected_mismatch"),
    [
        ("a.jpg", False),
        ("a.jpeg", False),  # gleichwertige Endung
        ("a.JPG", False),  # Gross-/Kleinschreibung
        ("a.png", True),
        ("a", False),  # keine Endung -> kein Widerspruch feststellbar
    ],
)
def test_extension_mismatch_only_on_real_contradiction(
    filename: str, expected_mismatch: bool
) -> None:
    assert media.detect(JPEG, filename=filename).extension_mismatch is expected_mismatch


def test_unknown_iso_bmff_brand_is_not_guessed_as_video() -> None:
    """Unbekannter ftyp-Brand: ISO-BMFF ist belegt, der Untertyp nicht."""
    result = media.detect(b"\x00\x00\x00\x18ftypZZZZ", filename="a.bin")
    assert result.category is MediaCategory.OTHER
    assert "ISO-BMFF" in (result.format_name or "")


def test_falls_back_to_mime_database() -> None:
    result = media.detect(b"\x00\x01\x02\x03irgendwas", filename="a.mp3")
    assert result.detection_method is DetectionMethod.MIME
    assert result.mime_type == "audio/mpeg"


def test_plain_text_without_signature_is_recognised() -> None:
    result = media.detect(b"Hallo Welt\nZeile zwei\t", filename="notiz.txt")
    assert result.category is MediaCategory.DOCUMENT
    assert result.mime_type == "text/plain"


def test_binary_without_signature_or_known_extension_is_other() -> None:
    result = media.detect(b"\x01\x02\x00\xff\xfe", filename="a.unbekannt")
    assert result.category is MediaCategory.OTHER
    assert result.detection_method is DetectionMethod.EXTENSION


def test_completely_unknown_returns_unknown() -> None:
    result = media.detect(b"\x01\x02\x00\xff\xfe")
    assert result.detection_method is DetectionMethod.UNKNOWN
    assert result.category is MediaCategory.OTHER


def test_empty_content_is_unknown() -> None:
    assert media.detect(b"").detection_method is DetectionMethod.UNKNOWN


def test_detect_file_reads_from_disk(tmp_path: Path) -> None:
    target = tmp_path / "bild.jpg"
    target.write_bytes(JPEG + b"A" * 10_000)
    result = media.detect_file(target)
    assert result.format_name == "JPEG"
    assert result.category is MediaCategory.IMAGE


def test_detect_file_uses_supplied_filename(tmp_path: Path) -> None:
    """Der `relativePath` aus dem Backup zaehlt, nicht der fileID-Dateiname."""
    target = tmp_path / "c91efa0e"
    target.write_bytes(PNG)
    result = media.detect_file(target, filename="Documents/img/foto.txt")
    assert result.extension_mismatch


def test_categories_map_to_export_directories() -> None:
    assert MediaCategory.IMAGE.directory == "images"
    assert MediaCategory.VIDEO.directory == "videos"
    assert MediaCategory.AUDIO.directory == "audio"
    assert MediaCategory.DOCUMENT.directory == "documents"
    assert MediaCategory.ARCHIVE.directory == "documents"
    assert MediaCategory.DATABASE.directory == "databases"
    assert MediaCategory.OTHER.directory == "other"


# ---------------------------------------------------------------------------
# Pixelmasse
# ---------------------------------------------------------------------------


def _jpeg_with_size(width: int, height: int) -> bytes:
    """Ein JPEG-Geruest mit APP0, einem grossen APP1 und danach SOF0.

    Der grosse APP1-Block ist Absicht: echte Fotos tragen dort einen
    EXIF-Abschnitt mit eingebettetem Vorschaubild von einigen zehn Kilobyte.
    Wer nur die ersten Kilobyte liest, findet SOF0 nicht.
    """
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    payload = b"\x00" * 40_000
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    sof = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + app1 + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00"


def _png_with_size(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\r"
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


@pytest.mark.parametrize(("width", "height"), [(1, 1), (100, 73), (1920, 1080), (3840, 2160)])
def test_jpeg_dimensions_are_read_from_the_sof_segment(width: int, height: int) -> None:
    assert media.dimensions(_jpeg_with_size(width, height)) == (width, height)


def test_jpeg_dimensions_are_found_behind_a_large_exif_block() -> None:
    """Genau der Fall echter Fotos - 4 KB Kopf reichen dafuer nicht."""
    data = _jpeg_with_size(4032, 3024)
    assert len(data) > 40_000
    assert media.dimensions(data) == (4032, 3024)
    # Abgeschnitten vor dem SOF: keine Angabe, aber auch keine geratene.
    assert media.dimensions(data[:4096]) is None


@pytest.mark.parametrize(("width", "height"), [(1, 1), (320, 240), (2000, 3000)])
def test_png_dimensions(width: int, height: int) -> None:
    assert media.dimensions(_png_with_size(width, height)) == (width, height)


def test_gif_dimensions() -> None:
    data = b"GIF89a" + (64).to_bytes(2, "little") + (48).to_bytes(2, "little") + b"\x00" * 8
    assert media.dimensions(data) == (64, 48)


def test_webp_vp8x_dimensions() -> None:
    data = (
        b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 8
        + (511).to_bytes(3, "little") + (383).to_bytes(3, "little") + b"\x00" * 8
    )
    assert media.dimensions(data) == (512, 384)


@pytest.mark.parametrize(
    "data",
    [b"", b"nicht ein bild", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"%PDF-1.7\n"],
)
def test_unknown_or_truncated_gives_no_dimensions(data: bytes) -> None:
    """Keine Angabe ist richtig - eine geschaetzte waere falsch."""
    assert media.dimensions(data) is None


def test_zero_dimensions_are_rejected() -> None:
    assert media.dimensions(_png_with_size(0, 0)) is None
    assert media.dimensions(_jpeg_with_size(0, 0)) is None
