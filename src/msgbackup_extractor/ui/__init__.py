"""Lokales UI zum Durchsehen eines Exports.

Das UI ist eine einzelne, in sich geschlossene HTML-Datei im
Ausgabeverzeichnis. Es laedt nichts nach: kein CDN, keine externen Fonts,
keine Telemetrie. Bilder und Videos kommen ueber relative Pfade aus dem
Export selbst.

Der Index wird in die Seite eingebettet, statt ihn zur Laufzeit zu laden -
`fetch()` von `file://` scheitert an der Same-Origin-Regel der Browser.
"""

from msgbackup_extractor.ui.builder import build_index, render_page, write_page

__all__ = ["build_index", "render_page", "write_page"]
