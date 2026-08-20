"""Umgang mit Schluesselmaterial im Speicher.

Ziel ist es, das Zeitfenster zu verkleinern, in dem abgeleitete Schluessel im
Prozessspeicher liegen. Eine Garantie ist das nicht: CPython kann unveraender-
liche `bytes`/`str` beliebig kopieren, und das vom Nutzer eingegebene Passwort
existiert zwangslaeufig als `str`. Diese Grenze ist bewusst dokumentiert und
wird nicht wegargumentiert.

Der Modulname ist absichtlich nicht `secrets`, um das Standardbibliotheksmodul
gleichen Namens nicht zu verschatten.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, Self

#: Platzhalter, der in Reprs und Logausgaben anstelle des Werts erscheint.
REDACTED: Final = "<redacted>"


class SecretWiped(RuntimeError):
    """Zugriff auf ein bereits geloeschtes Geheimnis."""


class SecretBytes:
    """Ein Byte-Geheimnis in einem ueberschreibbaren Puffer.

    Der Wert liegt in einem `bytearray`, das `wipe()` an seiner Speicherstelle
    mit Nullen ueberschreibt. Das Objekt gibt seinen Inhalt nie in `repr()`,
    `str()` oder beim Formatieren heraus - damit kann es nicht versehentlich in
    einer Logzeile landen.

    Als Context Manager loescht es sich beim Verlassen des Blocks:

        with SecretBytes(key) as k:
            do_something(k.reveal())
        # k ist hier geloescht
    """

    __slots__ = ("_buffer", "_wiped")

    def __init__(self, value: bytes | bytearray | memoryview) -> None:
        self._buffer: bytearray | None = bytearray(value)
        self._wiped = False

    # -- Zugriff ------------------------------------------------------------

    def reveal(self) -> bytes:
        """Gibt eine Kopie des Werts zurueck. Nur unmittelbar vor Gebrauch aufrufen."""
        return bytes(self._require())

    def view(self) -> memoryview:
        """Read-only-Sicht ohne Kopie. Wird durch `wipe()` ungueltig."""
        return memoryview(self._require()).toreadonly()

    def __len__(self) -> int:
        return len(self._require())

    @property
    def is_wiped(self) -> bool:
        return self._wiped

    def _require(self) -> bytearray:
        if self._wiped or self._buffer is None:
            raise SecretWiped("Dieses Geheimnis wurde bereits aus dem Speicher geloescht")
        return self._buffer

    # -- Loeschen -----------------------------------------------------------

    def wipe(self) -> None:
        """Ueberschreibt den Puffer mit Nullen. Idempotent."""
        buffer, self._buffer, self._wiped = self._buffer, None, True
        if buffer is None or not buffer:
            return
        try:
            address = (ctypes.c_char * len(buffer)).from_buffer(buffer)
            ctypes.memset(ctypes.byref(address), 0, len(buffer))
            del address
        except (TypeError, ValueError):  # pragma: no cover - Fallback
            for i in range(len(buffer)):
                buffer[i] = 0
        del buffer[:]

    # -- Protokolle ---------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.wipe()

    def __del__(self) -> None:
        try:
            self.wipe()
        except Exception:  # pragma: no cover - Interpreter-Shutdown
            pass

    # Kein Inhalt in Textausgaben. Das ist die eigentliche Schutzfunktion.
    def __repr__(self) -> str:
        state = "wiped" if self._wiped else f"{len(self._buffer or b'')} bytes"
        return f"SecretBytes({state}, {REDACTED})"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return repr(self)

    # Gleichheit nur in konstanter Zeit, und nie gegen beliebige Objekte.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretBytes):
            return NotImplemented
        import hmac

        return hmac.compare_digest(self.reveal(), other.reveal())

    __hash__ = None  # type: ignore[assignment]


@contextmanager
def transient_password(prompt: str = "Password: ") -> Iterator[str]:
    """Liest ein Passwort interaktiv und gibt die Referenz danach frei.

    Ausschliesslich `getpass`; kein CLI-Argument, keine Environment-Variable,
    keine Datei. Das `str`-Objekt selbst ist in CPython nicht sicher
    loeschbar - deshalb wird es so kurz wie moeglich gehalten und die Referenz
    beim Verlassen des Blocks geloescht.
    """
    import getpass

    password = getpass.getpass(prompt)
    if not password:
        raise ValueError("Es wurde kein Passwort eingegeben")
    try:
        yield password
    finally:
        del password
