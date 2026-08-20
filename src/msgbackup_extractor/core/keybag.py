"""Parsen des Backup-Keybags aus Manifest.plist.

Der Keybag ist ein TLV-Container: je Element vier ASCII-Zeichen als Tag, vier
Byte Laenge (big-endian), dann der Wert. Zuerst kommt ein Kopfteil, danach je
Protection Class ein Block.

Dies ist reines **Formatparsing**. Es findet hier keine Kryptografie statt; die
Schluesselableitung liegt in `encryption.py` und nutzt ausschliesslich Verfahren
aus `cryptography`.

Struktur:

    Kopf:    VERS TYPE UUID HMCK WRAP SALT ITER [DPSL DPIC]
    je Class: UUID CLAS WRAP KTYP WPKY

Abgrenzung der Blocks: das **erste** `UUID` gehoert zum Kopf, jedes weitere
beginnt einen neuen Class-Block. So machen es auch die etablierten
Referenzimplementierungen, und es ist robuster als eine Zaehlung fester
Feldreihenfolgen.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final

from msgbackup_extractor.core.logging_setup import get_logger

logger = get_logger("keybag")

TAG_LENGTH: Final = 4
HEADER_LENGTH: Final = 8

#: Tags, deren Wert eine 32-Bit-Ganzzahl ist.
_INTEGER_TAGS: Final = frozenset({"VERS", "TYPE", "WRAP", "ITER", "DPIC", "CLAS", "KTYP"})

#: Tags, die zu einem Class-Block gehoeren.
_CLASS_TAGS: Final = frozenset({"CLAS", "WRAP", "KTYP", "WPKY", "PBKY"})

#: Ab dieser Keybag-Version wird die doppelte PBKDF2-Ableitung verwendet.
DOUBLE_PBKDF_VERSION: Final = 3

#: Erwartete Laenge eines gewrappten 256-Bit-Schluessels (32 + 8 Byte Pruefwert).
WRAPPED_KEY_LENGTH: Final = 40

#: Groessenordnung, ab der ein Iterationswert als unplausibel gilt.
_MAX_ITERATIONS: Final = 100_000_000


class KeybagParseError(ValueError):
    """Der Keybag ist nicht auswertbar."""


@dataclass(frozen=True, slots=True)
class ClassKey:
    """Der gewrappte Schluessel einer Protection Class."""

    protection_class: int
    wrapped_key: bytes
    #: Wrap-Verfahren: Bit 1 = mit Passcode-Key gewrappt, Bit 2 = mit Device-Key.
    wrap: int = 0
    key_type: int = 0
    uuid: bytes | None = None

    @property
    def is_passcode_wrapped(self) -> bool:
        """True, wenn der Schluessel mit dem aus dem Passwort abgeleiteten Key gewrappt ist.

        Nur solche Schluessel sind aus einem Backup heraus entpackbar. Klassen,
        die ausschliesslich am Geraeteschluessel haengen, bleiben unzugaenglich -
        das wird berichtet, nicht verschwiegen.
        """
        return bool(self.wrap & 2) or self.wrap == 0


@dataclass(frozen=True, slots=True)
class Keybag:
    """Der geparste Backup-Keybag."""

    version: int
    keybag_type: int
    uuid: bytes | None
    salt: bytes
    iterations: int
    #: Nur bei Version >= 3 vorhanden (doppeltes PBKDF2).
    double_salt: bytes | None = None
    double_iterations: int | None = None
    wrap: int = 0
    hmac_key: bytes | None = None
    class_keys: dict[int, ClassKey] = field(default_factory=dict)

    @property
    def uses_double_derivation(self) -> bool:
        """Doppeltes PBKDF2 wird verwendet, wenn DPSL und DPIC vorhanden sind."""
        return self.double_salt is not None and self.double_iterations is not None

    @property
    def available_classes(self) -> tuple[int, ...]:
        return tuple(sorted(self.class_keys))

    @property
    def passcode_wrapped_classes(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                protection_class
                for protection_class, key in self.class_keys.items()
                if key.is_passcode_wrapped
            )
        )

    def __repr__(self) -> str:
        # Kein Schluesselmaterial in Textausgaben.
        return (
            f"Keybag(version={self.version}, type={self.keybag_type}, "
            f"classes={len(self.class_keys)}, iterations={self.iterations}, "
            f"double_derivation={self.uses_double_derivation})"
        )


def iterate_tlv(blob: bytes) -> list[tuple[str, bytes]]:
    """Zerlegt einen TLV-Block in seine Elemente.

    Raises:
        KeybagParseError: Bei abgeschnittenen oder unplausiblen Laengenangaben.
    """
    elements: list[tuple[str, bytes]] = []
    offset = 0
    total = len(blob)

    while offset < total:
        if offset + HEADER_LENGTH > total:
            raise KeybagParseError(
                f"Abgeschnittenes TLV-Element bei Offset {offset}: "
                f"nur {total - offset} von {HEADER_LENGTH} Kopfbytes vorhanden."
            )
        raw_tag = blob[offset : offset + TAG_LENGTH]
        try:
            tag = raw_tag.decode("ascii")
        except UnicodeDecodeError as error:
            raise KeybagParseError(
                f"Kein ASCII-Tag bei Offset {offset}: {raw_tag.hex()}"
            ) from error

        (length,) = struct.unpack(">I", blob[offset + TAG_LENGTH : offset + HEADER_LENGTH])
        value_start = offset + HEADER_LENGTH
        value_end = value_start + length
        if value_end > total:
            raise KeybagParseError(
                f"Element {tag} bei Offset {offset} gibt {length} Byte an, "
                f"es sind aber nur {total - value_start} vorhanden."
            )
        elements.append((tag, blob[value_start:value_end]))
        offset = value_end

    return elements


def _as_int(tag: str, value: bytes) -> int:
    if len(value) != 4:
        raise KeybagParseError(
            f"{tag} sollte 4 Byte lang sein, ist aber {len(value)} Byte."
        )
    return int.from_bytes(value, "big")


def parse_keybag(blob: bytes) -> Keybag:
    """Parst den Inhalt von `Manifest.plist:BackupKeyBag`.

    Raises:
        KeybagParseError: Wenn der Keybag fehlt, abgeschnitten ist oder die
            fuer die Schluesselableitung noetigen Felder nicht enthaelt.
    """
    if not blob:
        raise KeybagParseError(
            "Manifest.plist enthaelt keinen BackupKeyBag. Ohne Keybag ist ein "
            "verschluesseltes Backup nicht entschluesselbar."
        )

    header: dict[str, bytes] = {}
    class_blocks: list[dict[str, bytes]] = []
    current: dict[str, bytes] | None = None
    seen_header_uuid = False

    for tag, value in iterate_tlv(blob):
        if tag == "UUID":
            if not seen_header_uuid:
                seen_header_uuid = True
                header["UUID"] = value
            else:
                current = {"UUID": value}
                class_blocks.append(current)
            continue

        if current is not None and tag in _CLASS_TAGS:
            current[tag] = value
        else:
            header[tag] = value

    missing = [tag for tag in ("SALT", "ITER") if tag not in header]
    if missing:
        raise KeybagParseError(
            f"Dem Keybag fehlen die Felder {', '.join(missing)}. Damit ist die "
            "Schluesselableitung nicht moeglich."
        )

    iterations = _as_int("ITER", header["ITER"])
    if not 1 <= iterations <= _MAX_ITERATIONS:
        raise KeybagParseError(f"Unplausible Iterationszahl im Keybag: {iterations}")

    double_iterations: int | None = None
    if "DPIC" in header:
        double_iterations = _as_int("DPIC", header["DPIC"])
        if not 1 <= double_iterations <= _MAX_ITERATIONS:
            raise KeybagParseError(
                f"Unplausible Iterationszahl (DPIC) im Keybag: {double_iterations}"
            )

    class_keys: dict[int, ClassKey] = {}
    for block in class_blocks:
        if "CLAS" not in block or "WPKY" not in block:
            logger.debug("Class-Block ohne CLAS oder WPKY wird uebersprungen")
            continue
        protection_class = _as_int("CLAS", block["CLAS"])
        wrapped = block["WPKY"]
        if len(wrapped) != WRAPPED_KEY_LENGTH:
            logger.warning(
                "Protection Class %d hat einen Schluessel unerwarteter Laenge (%d Byte); "
                "sie wird als nicht verfuegbar behandelt",
                protection_class,
                len(wrapped),
            )
            continue
        class_keys[protection_class] = ClassKey(
            protection_class=protection_class,
            wrapped_key=wrapped,
            wrap=_as_int("WRAP", block["WRAP"]) if "WRAP" in block else 0,
            key_type=_as_int("KTYP", block["KTYP"]) if "KTYP" in block else 0,
            uuid=block.get("UUID"),
        )

    if not class_keys:
        raise KeybagParseError(
            "Der Keybag enthaelt keine verwertbaren Klassenschluessel. Damit "
            "koennen keine Dateien entschluesselt werden."
        )

    version = _as_int("VERS", header["VERS"]) if "VERS" in header else 0
    keybag = Keybag(
        version=version,
        keybag_type=_as_int("TYPE", header["TYPE"]) if "TYPE" in header else 0,
        uuid=header.get("UUID"),
        salt=header["SALT"],
        iterations=iterations,
        double_salt=header.get("DPSL"),
        double_iterations=double_iterations,
        wrap=_as_int("WRAP", header["WRAP"]) if "WRAP" in header else 0,
        hmac_key=header.get("HMCK"),
        class_keys=class_keys,
    )

    if version >= DOUBLE_PBKDF_VERSION and not keybag.uses_double_derivation:
        logger.warning(
            "Keybag-Version %d erwartet DPSL/DPIC, diese fehlen aber. Es wird die "
            "einfache Ableitung versucht.",
            version,
        )

    logger.debug("Keybag geparst: %r", keybag)
    return keybag
