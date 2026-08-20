# Messenger Backup Extractor

Lokales Kommandozeilenwerkzeug, das Messenger-Daten aus einem lokalen
Apple-iPhone-Backup identifiziert und in eine normale Dateistruktur exportiert.
Entwickelt und erprobt auf **macOS**; **Windows** ist implementiert, aber nicht
an einem echten Backup getestet (siehe [Voraussetzungen](#voraussetzungen)).

Unterstützt werden **Threema**, **WhatsApp** und **Signal**. Die App-Erkennung
ist als Plugin gebaut; ein weiterer Messenger braucht nur ein neues Profil.

> **Status:** in Entwicklung.
>
> | Befehl | Stand |
> |---|---|
> | `analyze` | fertig, auch für verschlüsselte Backups |
> | `database` | fertig, auch für verschlüsselte Backups |
> | `backups` | fertig |
> | `extract` | fertig, inkl. Chat-Zuordnung |
> | `verify` | fertig |
> | `ui` | fertig |
> | `collect` | fertig |
>
> | Messenger | Stand |
> |---|---|
> | Threema | vollständig, an echten Daten erprobt |
> | WhatsApp | vollständig, an echten Daten erprobt |
> | Signal | erkannt, aber **nicht extrahierbar** — siehe unten |
>
> An einem echten Backup erprobt: iPhone, iOS, [Menge entfernt], Threema
> [Version entfernt] — [Anzahl entfernt] Dateien / [Menge entfernt] extrahiert, 0 Fehler, 0
> Integritätsfehler, 93 % der Medien einem Chat zugeordnet.
>
> Die Extraktion wird gebaut, sobald die Analyse gegen ein echtes Backup
> geprüft ist — damit sie sich an der tatsächlich vorgefundenen Struktur
> orientiert und nicht an Annahmen.

## Grundsätze

Dieses Programm arbeitet **ausschließlich lokal**:

- keine Netzwerkverbindungen zur Laufzeit (durch Tests erzwungen)
- keine Cloud-Dienste, keine Telemetrie, keine Analytics
- das Apple-Backup wird **nur lesend** geöffnet und niemals verändert
- geschrieben wird ausschließlich in das mit `--output` angegebene Verzeichnis
- Passwörter nur über `getpass`, nie als CLI-Argument, nie in Logs
- keine Nachrichteninhalte in Logausgaben, auch nicht mit `--verbose`

Details zum Sicherheitsmodell und zur Architektur:
[`docs/specs/2026-08-20-messenger-backup-extractor-design.md`](docs/specs/2026-08-20-messenger-backup-extractor-design.md)

## Wie es funktioniert

Sechs Schritte, jeder ein eigener Befehl. Man kann nach jedem aufhören.

```
  iPhone
    │  Finder-Backup (lokal, verschlüsselt oder nicht)
    ▼
  ~/Library/Application Support/MobileSync/Backup/<UDID>
    │
    │  msgx backups     Welche Backups liegen hier?
    │  msgx analyze     Was steckt drin? (nur lesen, nichts schreiben)
    │  msgx database    Wie sieht das Schema der App-Datenbank aus?
    ▼
  msgx extract  ──────────────────────────────────────┐
    │                                                 │
    │  liest das Backup ausschließlich lesend         │
    ▼                                                 │
  ~/messenger-extract/export/threema/                 │
    ├── media/ chats/ databases/ metadata/            │
    └── export-manifest.json  ◄─── die Nachweisebene ─┘
    │
    │  msgx ui        erzeugt index.html aus dem Manifest
    │  msgx verify    prüft den Export gegen das Manifest
    ▼
  index.html im Browser
    │  auswählen, „Auswahl übernehmen …“, Liste kopieren
    ▼
  msgx collect     trägt die Auswahl in einen Ordner zusammen
```

### Was in den einzelnen Schritten passiert

**1. Backup öffnen.** Das Backup besteht aus vier Metadatendateien und 256
Unterverzeichnissen `00`–`ff`, in denen die Nutzdateien unter ihrer `fileID`
liegen (SHA-1 von `"<domain>-<relativePath>"`). `Info.plist` und
`Manifest.plist` sind auch bei verschlüsselten Backups im Klartext — daher
funktioniert die App-Erkennung ohne Passwort.

**2. Entschlüsseln, falls nötig.** Aus `Manifest.plist` kommt der Keybag; daraus
wird per PBKDF2 der Passcode-Key abgeleitet, mit dem die Klassenschlüssel per
AES-Key-Wrap entpackt werden. `Manifest.db` wird damit in ein temporäres
Verzeichnis **außerhalb** des Backups entschlüsselt. Ein falsches Passwort
scheitert an der Integritätsprüfung des Key-Wrap — es entsteht kein Datenmüll.

**3. Messenger erkennen.** Nicht über geratene Pfade, sondern über den Bundle
Identifier, der in den Backup-Metadaten tatsächlich steht. Gesucht wird ein
Namensraum (`ch.threema.`), damit auch Varianten gefunden werden; mehrere
Treffer führen zur Rückfrage, nicht zu einer Auswahl.

**4. Medien auffinden.** Hier liegt die eigentliche Arbeit. Threema speichert
über Core Data, und Mediendaten stecken an **zwei** Orten: als Datei in
`.ThreemaData_SUPPORT/_EXTERNAL_DATA/` oder als Blob **in** der Datenbank. Wer
nur Dateien kopiert, verliert die zweite Sorte stillschweigend — im
vermessenen Backup waren das 714 Originale. Die Zuordnung zu Chats läuft über
`ZMESSAGE` → `ZCONVERSATION`; welche Richtung einer Beziehung den
Fremdschlüssel trägt, wird zur Laufzeit **gemessen**, weil Core Data das je
Entität unterschiedlich ablegt.

**5. Extrahieren.** Ein Planer rechnet zuerst aus, was entstehen würde — das ist
die Grundlage von `--dry-run` *und* des echten Laufs, damit der Probelauf sich
nicht anders verhalten kann. Dann wird jede Datei einzeln geschrieben: der
SHA-256 der Quelle entsteht beim Schreiben, der des Ziels wird danach
nachgelesen, und erst der Vergleich gilt als Nachweis. Eine kaputte Datei
kostet einen Eintrag im Bericht, nicht den Lauf.

**6. Ansehen und herausholen.** `msgx ui` erzeugt eine in sich geschlossene
`index.html` aus dem Manifest. Auswählen geschieht im Browser, das Kopieren
macht wieder die CLI — JavaScript darf auf einer `file://`-Seite lokale Dateien
anzeigen, aber ihre Bytes nicht lesen.

### Die unterstützten Messenger

Jeder Messenger legt seine Daten anders ab. Was sie unterscheidet, ist keine
Kleinigkeit — es entscheidet, ob eine Extraktion überhaupt funktioniert:

| | Threema | WhatsApp |
|---|---|---|
| Datenbank | `ThreemaData.sqlite` | `ChatStorage.sqlite` |
| Medien | Blobs, teils **in** der Datenbank | **Dateien** unter `Message/Media/` |
| Referenz | `0x02` + UUID → `_EXTERNAL_DATA/` | Pfad in der DB, Präfix `Message/` fehlt |
| Vorschaubilder | Blob in `ZIMAGEDATA` | Datei über `ZXMPPTHUMBPATH` |
| Chat | `ZCONVERSATION` | `ZWACHATSESSION.ZPARTNERNAME` |
| Beziehungsrichtung | eine Seite **verwaist** | beide tragen |

Beide sind Core-Data-Stores und teilen deshalb die Zeitrechnung (ab 2001) und
das Vermessen der Beziehungsrichtungen. Dass die Richtung **gemessen** und nicht
angenommen wird, ist bei Threema notwendig: dort ist `ZIMAGEDATA.ZMESSAGE` zu
100 % verwaist, und wer dort joint, hält die Chat-Zuordnung für unmöglich.

#### Signal ist nicht extrahierbar

Signal wird erkannt, aber es gibt nichts zu holen: die App schließt ihr
Datenverzeichnis vom iOS-Backup aus. Im gemessenen Backup lagen in fünf
Signal-Domains insgesamt **zwölf Dateien mit 41 KB** — Einstellungs-Plists,
WebKit-Caches, eine Lock-Datei. Keine Nachrichtendatenbank, keine Medien.

Das Profil existiert genau deshalb: damit der Bericht den Grund nennt, statt
dass ein leeres Ergebnis wie ein Fehler dieses Programms aussieht. Signal-Daten
überträgt man mit Signals eigenem Weg (Gerätewechsel oder Signal-Backup).

### Was das Programm nie tut

Das Backup wird **nur gelesen**. SQLite-Verbindungen laufen mit
`mode=ro&immutable=1`, damit nicht einmal eine `-wal`-Datei daneben entsteht.
Geschrieben wird ausschließlich in das mit `--output` angegebene Verzeichnis,
und ein Guard prüft jeden Zielpfad dagegen. Belegt ist das durch einen Test,
der einen Fingerabdruck des gesamten Backups vor und nach einem vollen
Extraktionslauf vergleicht — samt Gegenproben, dass der Fingerabdruck
Veränderungen wirklich erkennt.

Es gibt **keine Netzwerkfunktionalität**. Kein Modul des Pakets importiert
`socket`, `urllib`, `http` oder Ähnliches; ein Test prüft das statisch und
zusätzlich dynamisch mit gesperrtem `socket`.

### Wo was liegt

| Datei | Aufgabe |
|---|---|
| `core/backup.py` | einziger Besitzer des Backup-Pfads, alles read-only |
| `core/keybag.py`, `core/encryption.py` | Keybag-Parsing und Entschlüsselung |
| `core/manifest.py` | `Manifest.db` mit Schema-Introspektion |
| `core/session.py` | bündelt Passwort, Keybag und Manifest-Zugriff |
| `core/media.py` | Magic Bytes vor MIME vor Endung |
| `core/paths.py` | Traversal-Abwehr, Output-Guard, Cloud-Guard |
| `apps/base.py`, `apps/threema.py` | Messenger-Profile als Plugins |
| `extract/planner.py`, `extract/runner.py` | Plan rechnen, dann ausführen |
| `extract/collect.py`, `extract/verify.py` | Auswahl einsammeln, Export prüfen |
| `ui/builder.py`, `ui/template.html` | die lokale Ansicht |

Die vollständige Architektur samt aller Entscheidungen und der am echten Backup
gemessenen Befunde steht in
[`docs/specs/2026-08-20-messenger-backup-extractor-design.md`](docs/specs/2026-08-20-messenger-backup-extractor-design.md).

## Voraussetzungen

| | macOS | Windows |
|---|---|---|
| Betriebssystem | macOS (entwickelt und geprüft) | Windows 10 oder 11 (siehe Einschränkung unten) |
| Python | 3.12 oder neuer | 3.12 oder neuer |
| Backup | lokales Finder-Backup | lokales Backup aus der Apple-Geräte-App oder iTunes |
| Zusätzlich | „Festplattenvollzugriff" für das Terminal | nichts — die Backups liegen im Benutzerprofil |
| Platz | etwa so viel wie das Backup groß ist | ebenso |

### Zum Platzbedarf

Der Export ist etwa so groß wie die Daten des Messengers im Backup. Die
Chat-Struktur kostet **nichts** zusätzlich, weil sie per Hardlink auf dieselben
Daten zeigt — im erprobten Fall sparte das [Menge entfernt] bei Threema und [Menge entfernt] bei
WhatsApp. Das Backup selbst bleibt unangetastet und muss nicht kopiert werden.

### Einschränkung: Windows ist nicht erprobt

Entwickelt und an einem echten Backup geprüft wurde ausschließlich auf **macOS**.
Der Kern ist plattformunabhängig — SQLite, plistlib, hashlib, `cryptography`
und das Formatparsing laufen überall, wo Python läuft. Betriebssystemabhängig
sind genau vier Dinge, und die stehen gesammelt in `core/platforms.py`: wo
Backups liegen, welche Ordner in die Cloud synchronisiert werden, wie die
Zwischenablage in eine Pipe kommt, und was bei fehlenden Rechten zu tun ist.

Die Windows-Pfade stammen aus Apples Dokumentation und dem üblichen Verhalten
der Apple-Geräte-App, **nicht aus einem Testlauf auf einem Windows-Rechner**.
Tests täuschen das Betriebssystem vor und prüfen, dass die richtigen Pfade und
Hinweise herauskommen; dass Apple die Backups dort tatsächlich ablegt, können
sie nicht bestätigen.

Findet das Programm auf Windows kein Backup, hilft `--backup` mit dem vollen
Pfad — der restliche Ablauf ist davon unabhängig. Wenn du es dort ausprobierst,
ist ein Bericht willkommen.

## Installation

### macOS

```bash
# 1. Python 3.12 prüfen
python3 --version

# 2. Projekt holen
git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

# 3. Virtuelle Umgebung anlegen — NICHT in iCloud Drive, siehe unten
python3 -m venv ~/.venvs/msgbackup-extractor
~/.venvs/msgbackup-extractor/bin/pip install -e ".[dev]"

# 4. Prüfen
~/.venvs/msgbackup-extractor/bin/msgx --version
~/.venvs/msgbackup-extractor/bin/python -m pytest
```

Bequemer wird es mit einem Alias in `~/.zshrc`:

```bash
alias msgx="$HOME/.venvs/msgbackup-extractor/bin/msgx"
```

**„Festplattenvollzugriff" erteilen.** Ohne diese Berechtigung kann das
Terminal `~/Library/Application Support/MobileSync/Backup/` nicht lesen:
Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff →
Terminal hinzufügen → **Terminal beenden und neu starten**. Der letzte Schritt
ist nötig, die Änderung greift nicht im laufenden Prozess.

### Windows

```powershell
# 1. Python 3.12 prüfen (aus python.org oder dem Microsoft Store)
py --version

# 2. Projekt holen
git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

# 3. Virtuelle Umgebung anlegen
py -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"

# 4. Prüfen
.\.venv\Scripts\msgx --version
.\.venv\Scripts\python -m pytest
```

Danach zeigt

```powershell
.\.venv\Scripts\msgx backups
```

welche Backups gefunden wurden. Gesucht wird an beiden üblichen Orten, weil
iTunes und die Apple-Geräte-App unterschiedliche Verzeichnisse verwenden:

```text
%APPDATA%\Apple Computer\MobileSync\Backup     (iTunes)
%USERPROFILE%\Apple\MobileSync\Backup          (Apple-Geräte-App)
```

Findet es nichts, nennt die Meldung beide Pfade samt Zustand. Dann hilft der
volle Pfad:

```powershell
.\.venv\Scripts\msgx analyze --backup "C:\Users\DU\Apple\MobileSync\Backup\<UDID>"
```

**Bei der Auswahl aus der Ansicht** heißt der Befehl für die Zwischenablage
unter Windows anders; der Übergabedialog nennt automatisch den richtigen:

```powershell
powershell -Command Get-Clipboard | msgx collect --output EXPORT --target ZIEL --selection -
```

### Wichtig auf macOS: venv nicht in iCloud Drive anlegen

Liegt das virtuelle Environment innerhalb von
`~/Library/Mobile Documents/com~apple~CloudDocs/` (iCloud Drive), setzt der
iCloud-File-Provider auf allen `.pth`-Dateien das macOS-Flag `UF_HIDDEN`.
Python 3.12 überspringt versteckte `.pth`-Dateien:

```python
# CPython Lib/site.py, addpackage()
if ((getattr(st, 'st_flags', 0) & stat.UF_HIDDEN) or ...):
    _trace(f"Skipping hidden .pth file: {fullname!r}")
    return
```

Folge: der Editable-Install wird **stillschweigend ignoriert** und
`import msgbackup_extractor` schlägt fehl. `chflags nohidden` hilft nicht — das
Flag wird binnen Sekunden neu gesetzt. Deshalb liegt das venv außerhalb von
iCloud Drive, auch wenn der Quellcode dort liegt. Nachprüfbar mit:

```bash
PYTHONVERBOSE=1 python -c "pass" 2>&1 | grep "Skipping hidden"
```

## Ablage der Daten

Backup und Export gehören in ein **lokales, nicht synchronisiertes**
Verzeichnis. Das Apple-Backup ist die gemeinsame Quelle für alle Messenger, die
Exporte werden pro Messenger getrennt:

```
~/messenger-extract/
├── backup/            # das Apple-Backup (read-only)
│   └── <UDID>/
└── export/
    ├── threema/
    ├── whatsapp/
    └── signal/
```

Unter Windows entsprechend, etwa `C:\Users\DU\messenger-extract\`.

Das Programm bricht ab, wenn `--output` in einem Cloud-Sync-Container liegt.
Erkannt werden je System die üblichen Ablagen:

| | erkannt als synchronisiert |
|---|---|
| macOS | `~/Library/Mobile Documents` (iCloud Drive), `~/Library/CloudStorage` |
| Windows | `%USERPROFILE%\iCloudDrive`, `%USERPROFILE%\OneDrive` |
| überall | Dropbox, OneDrive, Google Drive, pCloud, Nextcloud, ownCloud, Seafile, MEGA, Sync.com |

Sonst würde das Betriebssystem die extrahierten Daten selbsttätig hochladen —
das Programm selbst kommuniziert nicht, das Ergebnis wäre aber dasselbe. Mit
`--allow-cloud-output` lässt sich das bewusst erzwingen.

Die Erkennung ist rein pfadbasiert und damit offline. Einen beliebig
konfigurierten Sync-Ordner kann sie nicht kennen — das bleibt deine Aufgabe.

## Verwendung

### Backups finden

```bash
msgx backups
```

Listet die Backups unter `~/Library/Application Support/MobileSync/Backup/`
mit Gerätename, iOS-Version und Verschlüsselungszustand.

### Analysieren

```bash
msgx analyze --backup "~/messenger-extract/backup/<UDID>"
```

Der Bericht nennt Gerät, iOS-Version, Verschlüsselungszustand, erkannte
Messenger mit Bundle Identifier und Version, die zugehörigen Domains, die
gefundenen Medienformate und die identifizierten SQLite-Datenbanken.

Nützliche Optionen:

| Option | Wirkung |
|---|---|
| `--app threema` | nur einen Messenger prüfen |
| `--bundle-id ID` | eine mehrdeutige Erkennung auflösen |
| `--metadata-only` | bei verschlüsseltem Backup nicht nach dem Passwort fragen |
| `--json PFAD` | Bericht zusätzlich als JSON schreiben |
| `--include-schema` | vollständiges Manifest-Schema in den JSON-Bericht |
| `--no-media-inspection` | keine Nutzdateien lesen (schneller, ohne Formatstatistik) |
| `--verbose` | technische Details, weiterhin ohne Nachrichteninhalte |
| `--show-paths` | Dateipfade im Klartext statt maskiert |

### Extrahieren

Erst ein Probelauf — er schreibt nichts:

```bash
msgx extract \
    --backup "~/messenger-extract/backup/<UDID>" \
    --output "~/messenger-extract/export/threema" \
    --dry-run
```

Dann echt:

```bash
msgx extract \
    --backup "~/messenger-extract/backup/<UDID>" \
    --output "~/messenger-extract/export/threema"
```

Probelauf und echter Lauf verwenden **denselben Plan**; der Probelauf kann sich
also nicht anders verhalten als der Ernstfall. Was er nicht tut: Inhaltshashes
bilden — dafür müsste er alle Daten lesen und wäre so teuer wie der echte Lauf.
Duplikate werden daher erst beim echten Export erkannt.

Ergebnisstruktur:

```
export/threema/
├── media/
│   ├── images/  videos/  audio/  documents/  other/
│   └── thumbnails/          Vorschaubilder der App, mit Verweis aufs Original
├── chats/
│   ├── <Chatname>/{images,videos,audio,documents,thumbnails}/
│   └── unassigned/          alles ohne belegbare Zuordnung
├── databases/               die App-Datenbanken
├── metadata/                App-Interna (plists, Logs)
├── reports/extraction-report.json
└── export-manifest.json
```

`media/` und `chats/` zeigen per **Hardlink** auf dieselben Daten und belegen
den Speicher deshalb nur einmal. Im erprobten Export sparte das [Menge entfernt].

Optionen:

| Option | Wirkung |
|---|---|
| `--dry-run` | schreibt nichts, zeigt nur an |
| `--no-organize-by-chat` | nur `media/`, keine Chat-Struktur |
| `--no-hardlinks` | Kopien statt Hardlinks (doppelter Speicherbedarf) |
| `--no-thumbnails` | Vorschaubilder nicht exportieren |
| `--deduplicate` | inhaltsgleiche Dateien nur einmal schreiben |
| `--types image,video` | nur diese Kategorien |
| `--no-ui` | die lokale Ansicht **nicht** neu erzeugen |
| `--allow-cloud-output` | Ausgabe in einem Sync-Ordner erzwingen |

#### Die Ansicht wird automatisch neu erzeugt

Nach einem erfolgreichen Export schreibt `extract` die `index.html` im
Ausgabeverzeichnis neu. Du musst `msgx ui` also nicht von Hand aufrufen.
Abschalten mit `--no-ui`; bei `--dry-run` passiert es ohnehin nicht.

Für die **gemeinsame Übersicht** gilt eine Einschränkung, und die hat einen
Grund: sie liegt im Elternverzeichnis von `--output` und damit *außerhalb*
dessen, wohin dieses Programm zugesagt hat zu schreiben. Deshalb:

| Lage im Elternverzeichnis | Was `extract` tut |
|---|---|
| dort liegt schon eine von `msgx ui` erzeugte `index.html` | sie wird **mit aktualisiert** — du hast dieses Verzeichnis bereits als Übersichtsort bestimmt |
| dort liegt **keine** `index.html`, aber weitere Exporte | es wird **nichts** geschrieben; der Bericht nennt den Befehl `msgx ui --output …` |
| dort liegt eine **fremde** `index.html` | sie bleibt unangetastet — sie zu ersetzen wäre Datenverlust |

Erkannt wird eine eigene Seite an `<meta name="generator"
content="msgbackup-extractor">` im Dateikopf. Seiten, die mit einer älteren
Version erzeugt wurden, tragen diese Kennung nicht und werden deshalb nicht
angefasst; ein einmaliges `msgx ui --output …` bringt sie auf den neuen
Stand, danach greift die Automatik.

Scheitert das Erzeugen der Ansicht, ist das ein **Hinweis, kein Fehler**: die
Dateien sind zu diesem Zeitpunkt schon geschrieben und ihre Hashes geprüft.
Der Export bleibt gültig, und `msgx ui` lässt sich jederzeit nachholen.

### Ansehen

```bash
# Ein Messenger
msgx ui --output "~/messenger-extract/export/threema"

# Alle Messenger auf einer Seite
msgx ui --output "~/messenger-extract/export"
```

Erzeugt `index.html`. Doppelklick genügt — es braucht keinen Server.

Nach einem `extract` brauchst du das normalerweise **nicht**: die Ansicht wird
dabei automatisch neu erzeugt (siehe oben). `msgx ui` von Hand ist nötig, wenn
du die gemeinsame Übersicht zum ersten Mal anlegst, wenn du `--no-ui` verwendet
hast, oder wenn du eine Seite aus einer älteren Version auffrischen willst.

Zeigt `--output` auf ein **Exportverzeichnis**, entsteht eine Seite für diesen
Messenger. Zeigt es auf ein Verzeichnis, das mehrere Exporte enthält, entsteht
**eine gemeinsame Seite** mit einem Umschalter oben rechts (`Alle | Threema |
WhatsApp`) und dem Messenger als weiterer Filterdimension. Chatzeilen tragen
dann eine Messenger-Kennzeichnung, weil sich Chatnamen zwischen Messengern
wiederholen können.

Die Seite ist **in sich geschlossen**: kein CDN, keine externen Fonts, keine
Netzverbindung, keine Telemetrie. Symbole sind eingebettetes SVG statt Emoji,
damit sie überall gleich aussehen. Der Index wird in die Seite eingebettet und
nicht zur Laufzeit geladen, weil `fetch()` von `file://` an der
Same-Origin-Regel der Browser scheitert. Bilder und Videos kommen über relative
Pfade aus dem Export selbst. Ein Test prüft die Seite auf Netzverweise.

Aufbau:

- **Zeitachse**, neueste zuerst, mit Monatstrennern
- **Filter** für Medientyp, Jahr, Chat und Besonderheiten (ohne Chat, ohne
  Datum, nur Vorschaubild, Endung ≠ Inhalt) sowie Suche im Dateinamen.
  Die Zahlen sind **facettiert**: sie zeigen, was übrig bliebe, wenn du diese
  Option wählst — unter Berücksichtigung aller anderen Filter. Wählst du 2025,
  steht bei „Videos" nicht mehr 179, sondern 20. Optionen, die zu nichts führen,
  werden abgeblendet, statt eine Zahl zu zeigen, die in eine leere Ansicht
  führt. Eine Gruppe schränkt sich dabei nicht selbst ein — sonst zeigten alle
  nicht gewählten Jahre 0 und man käme nicht mehr weg.
- **Einzelansicht** mit Original, Videoplayer, Metadaten und Tastaturnavigation
  (`←` `→` blättern, `Leer` auswählen, `Esc` schließen)
- **Auswahl**: Häkchen auf jeder Kachel, mit gedrückter Umschalttaste ein ganzer
  Bereich, „Alle im Filter auswählen" für den gerade aktiven Filter. Die Auswahl
  wird nach Pfad gehalten und übersteht einen Filterwechsel.

Die **Sammelleiste** am unteren Rand erscheint, sobald es etwas zu tun gibt —
also bei bestehender Auswahl *oder* sobald ein Filter greift. Sie nennt den
aktiven Filter („Videos · 2023"), damit klar ist, worauf sich „Alle im Filter
auswählen (42)" bezieht. Ohne Auswahl sind „Auswahl aufheben" und „Auswahl
übernehmen" ausgegraut. „Filter zurücksetzen" in der Seitenleiste lässt die
Auswahl unangetastet — das sind zwei verschiedene Absichten.

Zwei Entscheidungen, die dort bewusst so sind:

**Kacheln zeigen die passende Auflösung.** Die von den Messengern
gespeicherten Vorschaubilder sind teils winzig — am gemessenen Export bei
WhatsApp im Median **100 × 73 Pixel**, bei Threema 384 Pixel kürzere Seite. In
einer Galeriekachel von rund 200 CSS-Pixeln wäre das auf einem Retina-Display
eine vier- bis fünffache Hochskalierung, also sichtbar unscharf.

Deshalb trägt das Export-Manifest die **echten Pixelmaße** jeder Datei, aus dem
Dateikopf gelesen (JPEG-SOF, PNG-IHDR, GIF, WEBP — Formatparsing, keine
Bildbibliothek). Die Seite bietet jeder Kachel beide Auflösungen per `srcset`
an und lässt den Browser je Kachel und Bildschirmdichte selbst wählen. Er nimmt
die kleinste hinreichende Quelle — das ist seine Aufgabe und er macht es besser
als eine feste Regel im Generator. Die Statuszeile nennt, wie viele Kacheln auf
das Original zurückgreifen mussten.

**Vorschaubilder sind keine eigenen Einträge.** Sie sind die Kachel ihres
Originals — sonst stünde jedes Bild doppelt in der Galerie. Ein Vorschaubild
*ohne* Original bleibt ein eigener Eintrag und ist als „nur Vorschau"
gekennzeichnet: es ist häufig alles, was von einem gelöschten Medium übrig ist.

**Auf der Zeitachse steht nur das Nachrichtendatum.** Für Dateien, die nur ein
Datei-Änderungsdatum aus dem Backup haben — Einstellungen, Logs, die
App-Datenbanken —, wäre dieses Datum irreführend: es liegt für alle am Tag des
Backups und hätte die neuesten echten Medien von der Spitze verdrängt. Sie
erscheinen deshalb unter „Ohne Datum", und die Einzelansicht weist ihr
Dateidatum getrennt aus.

Das UI wird **allein aus `export-manifest.json`** erzeugt. Es liest die
Threema-Datenbank nicht erneut, und `msgx ui` lässt sich jederzeit erneut
aufrufen, ohne neu zu extrahieren.

### Ausgewählte Dateien herausholen

Die Auswahl im UI führt über einen Dialog zu diesem Ablauf:

```bash
pbpaste | msgx collect \
    --output "~/messenger-extract/export/threema" \
    --target ~/Auswahl \
    --selection -
```

Der Dialog zeigt den fertigen Befehl mit deinen Pfaden und kopiert die Liste
der ausgewählten Pfade in die Zwischenablage. `--selection -` liest sie von der
Standardeingabe; alternativ nimmt `--selection DATEI` eine Textdatei mit einem
relativen Pfad je Zeile (Leerzeilen und `#`-Kommentare werden übergangen).

| Option | Wirkung |
|---|---|
| `--no-hardlinks` | Kopien statt Hardlinks — nötig, wenn du die Sammlung weitergeben willst |
| `--keep-structure` | Verzeichnisstruktur des Exports beibehalten statt flach zu sammeln |
| `--verify` | SHA-256 jeder Datei gegen das Export-Manifest prüfen |
| `--dry-run` | zeigt nur an, was gesammelt würde |

#### Warum das nicht der Browser macht

Ein „Als ZIP herunterladen"-Knopf ist auf einer `file://`-Seite **nicht
möglich**. Am echten Export gemessen:

```
fetch:            BLOCKIERT (TypeError: Failed to fetch)
XMLHttpRequest:   BLOCKIERT
<img> anzeigen:   OK
canvas auslesen:  BLOCKIERT (SecurityError — tainted canvas)
```

JavaScript darf lokale Dateien **anzeigen**, ihre Bytes aber nicht lesen — es
ist dieselbe Same-Origin-Regel, die auch das Einbetten des Index nötig macht.
Ein Knopf, der einen Download verspricht und nichts liefert, wäre schlechter
als kein Knopf. Deshalb wählt das UI aus und die CLI sammelt ein.

Standardmäßig entstehen **Hardlinks**: eine Sammlung von 937 MB belegt keinen
zusätzlichen Speicher, weil sie auf dieselben Daten zeigt wie der Export. Zum
Weitergeben `--no-hardlinks` verwenden oder die Sammlung anschließend packen.

### Prüfen

```bash
msgx verify --manifest "~/messenger-extract/export/threema"
```

Prüft Vorhandensein, Größe und SHA-256 jeder exportierten Datei sowie die
Hardlinks. Das Backup wird dafür **nicht** gebraucht — die Prüfung läuft auch
auf einer Kopie des Exports, Jahre später, auf einem anderen Rechner.

### Datenbankschemata ansehen

```bash
msgx database --backup "~/messenger-extract/backup/<UDID>"
```

Gibt Tabellen, Spalten, Typen, Primär- und Fremdschlüssel der gefundenen
App-Datenbanken aus — **keine Inhalte**.

## Verschlüsselte Backups

Der primäre Anwendungsfall. Das Programm

1. liest den Keybag aus `Manifest.plist`,
2. leitet daraus mit dem Passwort den Passcode-Key ab
   (PBKDF2, ab Keybag-Version 3 doppelt: SHA-1 dann SHA-256),
3. entpackt die Klassenschlüssel per AES-Key-Wrap (RFC 3394),
4. entschlüsselt `Manifest.db` mit AES-256-CBC in ein temporäres Verzeichnis
   **außerhalb** des Backups,
5. entschlüsselt Nutzdateien bei Bedarf — für die Typerkennung nur deren Anfang.

### Welches Passwort?

Das Passwort, das im Finder beim Aktivieren von „iPhone-Backup verschlüsseln"
gesetzt wurde. **Nicht** der Gerätecode des iPhones und **nicht** das
Apple-ID-Passwort. Ein falsches Passwort scheitert an der Integritätsprüfung
des AES-Key-Wrap und wird als solches gemeldet — es entsteht kein Datenmüll.

### Wie das Passwort abgefragt wird

Ausschließlich interaktiv über `getpass.getpass()`:

```text
Passwort des verschluesselten Backups:
```

Es gibt **kein** Kommandozeilenargument, **keine** Umgebungsvariable und
**keine** Konfigurationsdatei dafür — ein Passwort in `argv` landet in der
Shell-History und in der Prozessliste. Ein Test prüft per Introspektion aller
Unterbefehle, dass keine solche Option existiert.

Grenze, die offen benannt sein soll: CPython garantiert für unveränderliche
`str`/`bytes` kein sicheres Löschen. Abgeleitete Schlüssel liegen in einem
überschreibbaren Puffer und werden nach Gebrauch genullt; das eingegebene
Passwort selbst kann als String-Objekt im Heap verbleiben, bis der Prozess
endet. Das verkleinert das Zeitfenster, beseitigt es aber nicht.

### Ohne Passwort

`--metadata-only` erzeugt einen **Teilbericht** aus `Info.plist` und
`Manifest.plist`. Darin stehen Gerät, iOS-Version, Verschlüsselungszustand und
die erkannten Messenger samt Version — Datei- und Medienstatistiken fehlen und
werden ausdrücklich als fehlend ausgewiesen, nicht als Null dargestellt.

## Dependencies

| Paket | Version | Lizenz | Zweck |
|---|---|---|---|
| [`cryptography`](https://github.com/pyca/cryptography) | >=42,<46 | Apache-2.0 OR BSD-3-Clause | PBKDF2, AES-256-CBC, AES-Key-Wrap (RFC 3394) |
| `cffi` (transitiv) | — | MIT-0 | Bindings für `cryptography` |
| `pycparser` (transitiv) | — | BSD-3-Clause | Abhängigkeit von `cffi` |
| `pytest`, `pytest-cov` (nur dev) | — | MIT | Tests |

Alles Übrige kommt aus der Python-Standardbibliothek. Es wird **keine eigene
Kryptografie** implementiert: alle kryptografischen Primitive stammen aus
`cryptography`. Eigener Code beschränkt sich auf Formatparsing (TLV-Keybag,
NSKeyedArchiver-Plist).

Zur Laufzeit wird nichts nachgeladen.

## Tests

```bash
~/.venvs/msgbackup-extractor/bin/python -m pytest
```

Die Tests benötigen **niemals** echte private Daten. Alle Backups werden
synthetisch erzeugt (`tests/support/backup_builder.py`) — mit echtem TLV-Keybag,
echter PBKDF2-Ableitung, echtem AES-Key-Wrap, echter AES-256-CBC-Verschlüsselung
und echten NSKeyedArchiver-MBFile-Blobs, damit sie den Produktionscode
tatsächlich auf die Probe stellen und nicht nur zu einem vereinfachten
Testformat passen.

Besonders erwähnenswert sind drei Tests:

- `test_readonly_guarantee.py` nimmt einen vollständigen Fingerabdruck des
  Backups (Inhalt, Größe, mtime jeder Datei), führt alle lesenden Operationen
  aus und vergleicht erneut. Gegenproben belegen, dass der Fingerabdruck
  Veränderungen wirklich erkennt.
- `test_no_network.py` prüft statisch, dass keine Quelldatei ein Netzwerkmodul
  importiert, und dynamisch, dass sich das gesamte Paket mit gesperrtem `socket`
  importieren lässt.
- `test_analysis.py::test_encrypted_and_plain_reports_agree` analysiert dasselbe
  Backup einmal verschlüsselt und einmal unverschlüsselt und vergleicht die
  Berichte. Das ist der scharfe Test für die Entschlüsselung.

Linting:

```bash
~/.venvs/msgbackup-extractor/bin/ruff check src tests
```

## Troubleshooting

**`Kein Leserecht auf …` / `PermissionError` (macOS)**
Das Terminal braucht „Festplattenvollzugriff": Systemeinstellungen →
Datenschutz & Sicherheit → Festplattenvollzugriff → Terminal hinzufügen und
Terminal **neu starten**. Der Neustart ist nötig; die Änderung greift nicht im
laufenden Prozess.

**`Keine Backups gefunden` (Windows)**
Die Meldung nennt die durchsuchten Pfade samt Zustand. Häufige Ursachen: das
Backup liegt in iCloud statt lokal (dann gibt es keine Dateien zu lesen), oder
es wurde auf ein anderes Laufwerk verschoben. Mit dem vollen Pfad geht es
trotzdem:
`msgx analyze --backup "C:\Users\DU\Apple\MobileSync\Backup\<UDID>"`

**`pbpaste: command not found` (Windows)**
Das ist ein macOS-Befehl. Der Übergabedialog in der Ansicht nennt den für dein
System richtigen; unter Windows lautet er
`powershell -Command Get-Clipboard`.

**`Das Passwort ist falsch`**
Es ist das im Finder gesetzte Backup-Passwort, nicht der Gerätecode und nicht
das Apple-ID-Passwort.

**`… sieht nicht wie ein Apple-Backup aus`**
`--backup` muss auf das Verzeichnis mit der Geräte-ID zeigen, nicht auf
`MobileSync/Backup` selbst. `msgx backups` listet die Kandidaten.

**`Diagnosebericht: In der Manifest.db wurde keine Dateitabelle gefunden`**
Das Backup hat eine Struktur, die das Programm nicht kennt. Es bricht dann
bewusst ab, statt zu raten. Der Diagnosebericht listet die tatsächlich
vorhandenen Tabellen und Spalten — er ist die nützliche Grundlage für eine
Fehlermeldung.

**`import msgbackup_extractor` schlägt fehl**
Liegt das venv in iCloud Drive? Siehe den Abschnitt oben.

## Bekannte Einschränkungen

1. Dieses Programm kann nur Daten extrahieren, die tatsächlich im zugänglichen
   Apple-Backup vorhanden sind. Es garantiert nicht, dass jede historische
   Messenger-Datei vorhanden oder entschlüsselbar ist. Apps können Dateien vom
   Backup ausschließen.
2. Protection Classes, deren Schlüssel nur am Geräteschlüssel hängen, lassen
   sich aus einem Backup heraus nicht öffnen. Betroffene Dateien werden als
   „nicht entschlüsselbar" gezählt, nicht stillschweigend übersprungen.
3. Ein Manifest-Eintrag mit unlesbarem MBFile-Blob hat keinen Dateischlüssel. In
   einem verschlüsselten Backup ist sein Inhalt damit Chiffrat und sein Typ
   nicht bestimmbar. Er wird als „nicht entschlüsselbar" ausgewiesen, statt
   einen Typ aus Chiffrat zu erfinden.
4. Eine im Backup abgeschnittene Datei, deren Länge zufällig ein Vielfaches von
   16 Byte ist, entschlüsselt ohne Fehler — nur zu weniger Daten. Deshalb wird
   die entschlüsselte Byteanzahl immer gegen die im Manifest vermerkte Größe
   geprüft und eine Abweichung gemeldet.
5. Core Data stellt jedem Blob ein Markierungsbyte voran (`0x01` für Inline-
   Daten, `0x02` für eine Referenz auf `_EXTERNAL_DATA`). Es wird abgeschnitten;
   ein unerwarteter Wert wird gemeldet statt blind entfernt. Beginnt ein Blob
   nicht mit `0x01`, kann die betroffene Datei um ein Byte verschoben sein — das
   steht dann als Hinweis im Bericht.
6. Threema kann Teile seiner Daten zusätzlich app-eigen verschlüsseln. Ob und
   wie weit das eine Rolle spielt, ist erst nach der Analyse eines echten
   Backups beurteilbar und wird dann hier dokumentiert — nicht vorab versprochen.
7. Threema kann seine interne Struktur zwischen Versionen ändern. Das Programm
   erkennt Strukturen dynamisch, kann bei unbekannten Strukturen aber nur einen
   Diagnosebericht liefern statt Ergebnisse.
8. Sicheres Löschen des Passworts aus dem Python-Heap ist nicht garantierbar
   (siehe oben).
9. Der Cloud-Sync-Guard erkennt die üblichen Ablagen anhand des Pfads. Einen
   beliebig konfigurierten Sync-Ordner kann er nicht kennen.
10. **Windows ist implementiert, aber nicht erprobt.** Entwickelt und an einem
    echten Backup geprüft wurde ausschließlich macOS. Die Windows-Pfade stammen
    aus Apples Dokumentation, nicht aus einem Testlauf. Tests täuschen das
    Betriebssystem vor und prüfen die Pfadwahl; ob Apple die Backups dort
    tatsächlich ablegt, können sie nicht bestätigen.
11. Für andere Systeme (Linux, BSD) ist kein Standardort bekannt und es wird
    keiner erfunden. Ein kopiertes oder mit libimobiledevice erzeugtes Backup
    lässt sich über `--backup` trotzdem verarbeiten.

Weitere Details: §17 des Design-Dokuments.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
