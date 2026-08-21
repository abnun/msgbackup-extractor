# msgbackup-extractor

[English](README.md) · **Deutsch** · [Website](https://abnun.github.io/msgbackup-extractor/)

Holt die eigenen Fotos, Videos, Sprachnachrichten und Dokumente aus einem
lokalen Apple-iPhone-Backup — Threema und WhatsApp — ohne dafür etwas an einen
Cloud-Dienst zu geben.

Läuft vollständig auf dem eigenen Rechner, öffnet das Backup **nur lesend** und
prüft jede geschriebene Datei mit SHA-256. Wo es nicht sicher sein kann, sagt es
das, statt zu raten.

Entwickelt und geprüft auf **macOS**. **Windows** ist implementiert, aber nie an
einem echten Backup gelaufen — siehe [Voraussetzungen](#voraussetzungen).

```bash
msgx analyze  --backup "…/MobileSync/Backup/<UDID>"          # was ist drin?
msgx extract  --backup "…" --output ~/export/threema          # herausschreiben
msgx ui       --output ~/export                                # ansehen
```

---

## Was dabei herauskommt

Ein Ordner, den du behandeln kannst wie jeden anderen: Fotos, Videos,
Sprachnachrichten und Dokumente, zweifach sortiert — einmal nach Medienart,
einmal nach Chat — und eine Seite, die du doppelklickst, um alles durchzusehen.

```
export/threema/
├── media/
│   ├── images/  videos/  audio/  documents/
│   └── thumbnails/
├── chats/
│   ├── Anna/{images,videos,audio,documents}/
│   ├── Familie/…
│   └── unassigned/       bleibt sichtbar, wird nicht verworfen
├── databases/
└── index.html            doppelklicken, kein Server nötig
```

Die Chat-Ordner kosten **keinen** zusätzlichen Speicher — sie sind Hardlinks auf
dieselben Dateien. `index.html` ist eine Zeitleiste, neueste zuerst, mit Filtern
für Medienart, Jahr und Chat und einer Namenssuche. Sie ist in sich
geschlossen: kein Server, kein Netz, nichts zu installieren.

Was sie nicht kann, ist dir ein ZIP geben. Eine aus einer Datei geöffnete Seite
darf deine Dateien *anzeigen*, aber nicht *lesen* — du wählst also im Browser
aus, und `msgx collect` kopiert die Auswahl heraus.

Nachrichtentexte werden **nicht** exportiert.

---

## Wie es funktioniert

Sechs Schritte, jeder ein eigener Befehl. Nach jedem kann man aufhören.

```
  iPhone
    │  lokales Backup (Finder auf macOS, Apple-Geräte-App auf Windows)
    ▼
  MobileSync/Backup/<UDID>
    │
    │  msgx backups     welche Backups liegen auf diesem Rechner?
    │  msgx analyze     was ist drin? (nur lesend, schreibt nichts)
    │  msgx database    wie sieht das Datenbankschema der App aus?
    ▼
  msgx extract  ───────────────────────────────────────┐
    │  liest das Backup ausschließlich lesend          │
    ▼                                                  │
  export/threema/                                      │
    ├── media/ chats/ databases/ metadata/             │
    └── export-manifest.json  ◄──── das Protokoll ─────┘
    │
    │  msgx ui        baut index.html aus dem Manifest
    │  msgx verify    prüft den Export gegen das Manifest
    ▼
  index.html im Browser
    │  auswählen, „Auswahl übergeben", Liste kopieren
    ▼
  msgx collect     sammelt die Auswahl in einen Ordner
```

`export-manifest.json` ist die Drehscheibe: `extract` schreibt es, und
`verify`, `ui` und `collect` lesen nichts anderes. Deshalb kostet eine Änderung
am UI keinen neuen Export, und deshalb funktioniert `verify` noch Jahre später
auf einer Kopie ohne das Backup.

### Was in jedem Schritt passiert

**Backup öffnen.** Ein Backup besteht aus vier Metadatendateien und 256
Verzeichnissen `00` … `ff` mit Nutzdateien, benannt nach ihrer `fileID` (SHA-1
von `"<domain>-<relativePath>"`). `Info.plist` und `Manifest.plist` sind auch in
einem verschlüsselten Backup lesbar — darum funktioniert die App-Erkennung ohne
Passwort.

**Entschlüsseln, falls nötig.** Der Keybag kommt aus `Manifest.plist`, PBKDF2
leitet den Passcode-Schlüssel ab, AES-KeyWrap entpackt die Klassenschlüssel, und
`Manifest.db` wird in ein temporäres Verzeichnis **außerhalb** des Backups
entschlüsselt. Ein falsches Passwort scheitert an der Integritätsprüfung des Key
Wrap — es gibt eine klare Fehlermeldung, niemals Datenmüll.

**Messenger erkennen.** Nicht über geratene Pfade, sondern über den Bundle
Identifier, der tatsächlich in den Backup-Metadaten steht. Gesucht wird ein
Namensraum (`ch.threema.`), damit auch Varianten gefunden werden; mehrere Treffer
führen zu einer Rückfrage, nicht zu einer Auswahl.

**Medien finden.** Hier liegt die Arbeit. Threema speichert Blobs, teils
*innerhalb* seiner Datenbank; WhatsApp speichert echte Dateien und vermerkt
deren Pfade. Welche Seite einer Core-Data-Beziehung den Fremdschlüssel trägt,
wird **zur Laufzeit gemessen**, denn es unterscheidet sich je Entität.

**Extrahieren.** Ein Planer berechnet zuerst, was passieren würde — derselbe
Plan trägt `--dry-run` **und** den echten Durchlauf, damit die Probe sich nicht
anders verhalten kann als der Ernstfall. Dann wird jede Datei einzeln
geschrieben: Quelle beim Schreiben hashen, Ziel-Hash zurücklesen, vergleichen.
Eine kaputte Datei kostet einen Berichtseintrag, nicht den Durchlauf.

**Ansehen und herausholen.** `msgx ui` baut eine in sich geschlossene
`index.html` aus dem Manifest. Ausgewählt wird im Browser, kopiert wird vom CLI —
weil JavaScript auf einer `file://`-Seite lokale Dateien *anzeigen*, aber nicht
*lesen* darf.

### Was es nie tut

Das Backup wird **nur gelesen**. SQLite-Verbindungen laufen mit
`mode=ro&immutable=1`, damit nicht einmal eine `-wal`-Datei neben dem Original
entsteht. Geschrieben wird ausschließlich in das Verzeichnis aus `--output`, und
ein Wächter prüft jeden Zielpfad dagegen.

Es gibt **keinen Netzwerkcode**. Alles Plattformabhängige liegt in einem Modul
(`core/platforms.py`), und zwei Wächtertests halten es dort.

Die vollständige Architektur und jeder gemessene Befund:
[`docs/specs/2026-08-20-messenger-backup-extractor-design.md`](docs/specs/2026-08-20-messenger-backup-extractor-design.md)

---

## Unterstützte Messenger

| | Threema | WhatsApp | Signal |
|---|---|---|---|
| Stand | vollständig, an echten Daten geprüft | vollständig, an echten Daten geprüft | nur erkannt |
| Datenbank | `ThreemaData.sqlite` | `ChatStorage.sqlite` | nicht im Backup |
| Medien | Blobs, teils *in* der Datenbank | Dateien unter `Message/Media/` | — |
| Verweis | `0x02` + UUID → `_EXTERNAL_DATA/` | Pfad in der DB, ohne `Message/`-Präfix | — |
| Chatnamen | `ZCONVERSATION` | `ZWACHATSESSION.ZPARTNERNAME` | — |
| Beziehungsrichtung | eine Seite ist **vollständig verwaist** | beide Seiten tragen | — |

Beide sind Core-Data-Speicher, teilen also die Epoche (2001) und die Logik zum
Messen der Richtung. Messen statt annehmen ist nicht akademisch: bei Threema ist
`ZIMAGEDATA.ZMESSAGE` zu 100 % verwaist. Wer dort verknüpft, schließt daraus,
eine Chatzuordnung sei unmöglich.

### Signal lässt sich nicht extrahieren

Signal wird erkannt, aber es gibt nichts zu holen: die App schließt ihr
Datenverzeichnis vom iOS-Backup aus. Im hier gemessenen Backup enthielten fünf
Signal-Domains **eine Handvoll Dateien mit zusammen wenigen Dutzend Kilobyte** —
Einstellungs-Plists, WebKit-Caches, eine Lock-Datei. Keine
Nachrichtendatenbank, keine Medien.

Das Profil existiert genau dafür, das zu sagen, statt ein leeres Ergebnis wie
einen Fehler dieses Werkzeugs aussehen zu lassen. Signal-Daten gehen über
Signals eigenen Weg (Geräteübertragung oder ein Signal-Backup).

Einen Messenger zu ergänzen heißt, ein Profil zu schreiben. `AppProfile` hat
WhatsApp ohne Änderung überlebt, obwohl dessen Speichermodell grundlegend anders
ist.

---

## Voraussetzungen

| | macOS | Windows |
|---|---|---|
| Betriebssystem | macOS (hier entwickelt und geprüft) | Windows 10 oder 11 (siehe unten) |
| Python | 3.12 oder neuer | 3.12 oder neuer |
| Backup | lokales Finder-Backup | lokales Backup der Apple-Geräte-App oder von iTunes |
| Zusätzlich | Festplattenvollzugriff für das Terminal | nichts — Backups liegen im Benutzerprofil |
| Speicherplatz | etwa die Datenmenge des Messengers | dasselbe |

Die Chat-Struktur kostet **keinen** zusätzlichen Platz: sie ist per Hardlink auf
dieselben Daten gelegt. Ohne sie würde die Chat-Struktur den Platzbedarf
verdoppeln. Das Backup selbst wird nie kopiert.

### Windows ist ungeprüft

Der Kern ist plattformunabhängig: SQLite, plistlib, hashlib, `cryptography` und
das Parsen der Formate laufen überall, wo Python läuft. Genau vier Dinge hängen
am Betriebssystem, und alle liegen in `core/platforms.py`: wo Backups liegen,
welche Ordner in eine Cloud synchronisieren, wie die Zwischenablage in eine Pipe
kommt, und was bei fehlenden Rechten zu tun ist.

Die Windows-Pfade stammen aus Apples Dokumentation und dem üblichen Verhalten
der Apple-Geräte-App — **nicht aus einem Durchlauf auf einer
Windows-Maschine**. Die Tests täuschen das Betriebssystem vor und prüfen, dass
die richtigen Pfade und Hinweise herauskommen; sie können nicht bestätigen, dass
Apple Backups dort tatsächlich ablegt.

Wird kein Backup gefunden, funktioniert `--backup` mit vollem Pfad, und der Rest
der Kette ist davon unberührt. Ein Bericht von jemandem, der es probiert, ist
willkommen.

---

## Installation

### macOS

```bash
python3 --version                      # 3.12 oder neuer

git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

python3 -m venv ~/.venvs/msgbackup-extractor    # NICHT in iCloud Drive, siehe unten
~/.venvs/msgbackup-extractor/bin/pip install -e ".[dev]"

~/.venvs/msgbackup-extractor/bin/msgx --version
~/.venvs/msgbackup-extractor/bin/python -m pytest
```

Bequem mit einem Alias in `~/.zshrc`:

```bash
alias msgx="$HOME/.venvs/msgbackup-extractor/bin/msgx"
```

**Festplattenvollzugriff erteilen.** Ohne ihn kann das Terminal
`~/Library/Application Support/MobileSync/Backup/` nicht lesen:
Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff →
Terminal hinzufügen → **Terminal beenden und neu starten**. Der Neustart ist
nötig; die Änderung greift bei einem laufenden Prozess nicht.

**Das venv nicht in iCloud Drive legen.** Der iCloud-Dateianbieter setzt auf
jeder `.pth`-Datei das macOS-Flag `UF_HIDDEN`, und Python 3.12 überspringt
versteckte `.pth`-Dateien (`Lib/site.py`, `addpackage()`). Die
Editable-Installation wird dann **stillschweigend ignoriert**, und
`import msgbackup_extractor` scheitert. `chflags nohidden` hält nicht — das Flag
ist binnen Sekunden zurück. Prüfen mit:

```bash
PYTHONVERBOSE=1 python -c "pass" 2>&1 | grep "Skipping hidden"
```

### Windows

```powershell
py --version                           # 3.12 oder neuer

git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

py -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"

.\.venv\Scripts\msgx --version
.\.venv\Scripts\python -m pytest
```

`msgx backups` durchsucht beide üblichen Orte, denn iTunes und die
Apple-Geräte-App benutzen unterschiedliche Verzeichnisse:

```text
%APPDATA%\Apple Computer\MobileSync\Backup     (iTunes)
%USERPROFILE%\Apple\MobileSync\Backup          (Apple-Geräte-App)
```

Wird nichts gefunden, nennt die Meldung beide Pfade und ihren Zustand. Dann hilft
der vollständige Pfad:

```powershell
.\.venv\Scripts\msgx analyze --backup "C:\Users\DU\Apple\MobileSync\Backup\<UDID>"
```

---

## Wohin mit den Daten

Ein **lokales, nicht synchronisiertes** Verzeichnis, mit einem Unterverzeichnis
je Messenger. Das Apple-Backup ist die gemeinsame Quelle, die Exporte liegen
getrennt davon:

```
~/messenger-extract/
├── backup/            das Apple-Backup (nur lesend; ein Symlink genügt)
│   └── <UDID>/
└── export/
    ├── threema/
    ├── whatsapp/
    └── index.html     gemeinsame Ansicht über alle Exporte
```

Ein `--output` innerhalb eines Cloud-Ordners wird abgelehnt, denn das
Betriebssystem würde die extrahierten Nachrichten dann hochladen. Die üblichen
Orte je Plattform sind bekannt (iCloud Drive, OneDrive, Dropbox, Google Drive,
pCloud, Nextcloud, ownCloud, Seafile, MEGA, Sync.com). Die Erkennung arbeitet
über Pfade und ist damit offlinefähig; ein beliebig konfigurierter Sync-Ordner
liegt außerhalb ihrer Reichweite. `--allow-cloud-output` hebt die Ablehnung
bewusst auf.

---

## Verwendung

### Doppelklicken statt tippen

Für alle, die nicht selbst ein Terminal öffnen wollen, gibt es ein kleines
macOS-Bündel:

```bash
scripts/build-app.py            # legt msgbackup-extractor.app in ~/Applications
```

Es wird auf deinem Rechner aus Bordmitteln gebaut — nichts wird
heruntergeladen, kein Framework installiert. Das Symbol entsteht als PNG in
reinem Python, und `iconutil` von macOS macht daraus ein `.icns`.

**Es öffnet ein Terminal, und das ist Absicht.** Ein verschlüsseltes Backup
braucht ein Passwort, und das wird ausschließlich eingetippt — nie als Argument
übergeben, nie aus einem Schlüsselbund geholt, nie über ein Fenster
eingesammelt, das es irgendwohin schreiben müsste. Eine stille grafische
Oberfläche wäre hier ein Rückschritt. Das Bündel ist deshalb ein *Starter* und
keine zweite Anwendung.

Gestartet wird `msgx guide`, das man auch direkt aufrufen kann:

```bash
msgx guide
```

Es fragt nach Backup, Messenger und Ausgabeverzeichnis, führt `analyze` aus,
dann einen Probelauf, und extrahiert erst nach einem ausdrücklichen Ja.
**Jeder Schritt zeigt vorher den Befehl, den er ausführt** — es bringt dir das
Werkzeug also bei, statt es zu verstecken, und zuletzt nennt es den einen
Befehl, der beim nächsten Mal genügt.

Unter **Windows** schreibt derselbe Befehl stattdessen eine doppelklickbare
`msgbackup-extractor.cmd` auf den Desktop. Sie öffnet aus demselben Grund ein
Konsolenfenster. Wie die Windows-Unterstützung insgesamt ist sie dort nie
gelaufen. Auf jedem anderen System lehnt das Skript ab, statt etwas
Unbrauchbares anzulegen — `msgx guide` läuft dort direkt.

Der Pfad zu `msgx` wird beim Bauen fest eingesetzt, weil ein Bündel nicht wissen
kann, welche Umgebung gemeint ist. Wird sie verschoben oder gelöscht, sagt der
Starter das beim nächsten Start — statt lautlos nichts zu tun.

Der Bauer **probiert** jeden Kandidaten aus, statt sich auf sein Vorhandensein
zu verlassen: er ruft `msgx --version` auf und nimmt nur, was durchläuft. Das
ist keine Pedanterie — eine virtuelle Umgebung in iCloud Drive ist vorhanden und
ausführbar und scheitert trotzdem, ein Bündel darauf würde erst beim ersten
Doppelklick auffallen.

### Backups finden

```bash
msgx backups
```

Listet die Backups auf diesem Rechner mit Gerätename, iOS-Version und dem
Hinweis, ob sie verschlüsselt sind.

### Analysieren

```bash
msgx analyze --backup "~/messenger-extract/backup/<UDID>"
```

Berichtet Gerät, iOS-Version, Verschlüsselungszustand, erkannte Messenger mit
Bundle Identifier und Version, deren Domains, die tatsächlich gefundenen
Medienformate und die erkannten SQLite-Datenbanken. Schreibt nichts.

| Option | Wirkung |
|---|---|
| `--app threema` | nur einen Messenger prüfen |
| `--bundle-id ID` | eine mehrdeutige Erkennung auflösen |
| `--metadata-only` | nicht nach dem Passwort eines verschlüsselten Backups fragen |
| `--json PATH` | den Bericht zusätzlich als JSON schreiben |
| `--include-schema` | das vollständige Manifest-Schema ins JSON aufnehmen |
| `--no-media-inspection` | keine Nutzdateien lesen (schneller, keine Formatstatistik) |
| `--verbose` | technische Details; weiterhin keine Nachrichteninhalte |
| `--show-paths` | Dateipfade im Klartext statt maskiert |

### Extrahieren

Erst die Probe — sie schreibt nichts:

```bash
msgx extract --backup "~/messenger-extract/backup/<UDID>" \
             --output  "~/messenger-extract/export/threema" --dry-run
```

Dann ohne `--dry-run` im Ernst. Ergebnis:

```
export/threema/
├── media/
│   ├── images/ videos/ audio/ documents/ other/
│   └── thumbnails/          die Vorschauen der App, verlinkt mit ihren Originalen
├── chats/
│   ├── <Chatname>/{images,videos,audio,documents,thumbnails}/
│   └── unassigned/          alles ohne belegbare Zuordnung
├── databases/               die App-Datenbanken
├── metadata/                App-Interna (Plists, Logs)
├── reports/extraction-report.json
├── export-manifest.json
└── index.html               die lokale Ansicht, automatisch neu erzeugt
```

`media/` und `chats/` zeigen per **Hardlink** auf dieselben Daten, der Platz
wird also nur einmal belegt.

| Option | Wirkung |
|---|---|
| `--dry-run` | zeigen, was passieren würde, nichts schreiben |
| `--no-organize-by-chat` | nur `media/`, keine Chat-Struktur |
| `--no-hardlinks` | echte Kopien statt Hardlinks (doppelter Platz) |
| `--no-thumbnails` | die Vorschaubilder der App überspringen |
| `--deduplicate` | gleichen Inhalt nur einmal schreiben |
| `--types image,video` | auf diese Kategorien beschränken |
| `--no-ui` | die lokale Ansicht nicht neu erzeugen |
| `--allow-cloud-output` | Ausgabe in einen Sync-Ordner erzwingen |

**Die Ansicht wird automatisch neu erzeugt.** Nach einem erfolgreichen Export
schreibt `extract` die `index.html` im Ausgabeverzeichnis neu. Die
**gemeinsame** Ansicht liegt im übergeordneten Verzeichnis — außerhalb von
`--output`, wo dieses Werkzeug zugesagt hat, nicht zu schreiben. Also:

| Im übergeordneten Verzeichnis liegt | Was `extract` tut |
|---|---|
| eine von `msgx ui` erzeugte `index.html` | sie wird mit aktualisiert — dieses Verzeichnis war bereits als Übersicht gewählt |
| keine `index.html`, aber andere Exporte | es wird nichts geschrieben; der Bericht nennt den `msgx ui`-Befehl |
| eine **fremde** `index.html` | sie bleibt unangetastet — sie zu ersetzen wäre Datenverlust |

Erkannt wird an `<meta name="generator" content="msgbackup-extractor">` im
Dateikopf. Seiten einer älteren Version haben das nicht und bleiben unberührt;
ein Aufruf von `msgx ui` bringt sie auf den Stand.

Scheitert das Erzeugen der Ansicht, ist das ein **Hinweis, kein Fehler**: die
Dateien sind schon geschrieben und ihre Hashes geprüft.

### Ansehen

```bash
msgx ui --output "~/messenger-extract/export/threema"    # ein Messenger
msgx ui --output "~/messenger-extract/export"            # alle, eine Seite
```

Zeigt `--output` auf ein Exportverzeichnis, entsteht eine Seite für einen
Messenger; zeigt es auf ein Verzeichnis mit mehreren Exporten, entsteht **eine**
Seite mit Umschalter (`Alle | Threema | WhatsApp`) und dem Messenger als
zusätzlichem Filter.

Doppelklick auf die Datei. Kein Server nötig.

Die Seite ist **in sich geschlossen**: kein CDN, keine externen Schriften, kein
Netzwerkabruf, keine Telemetrie. Symbole sind eingebettetes SVG statt Emoji,
damit sie überall gleich aussehen. Der Index ist eingebettet statt abgerufen,
weil `fetch()` von `file://` an der Same-Origin-Regel des Browsers scheitert.
Ein Test prüft die Seite auf Netzverweise.

- **Zeitleiste**, neueste zuerst, mit Monatstrennern
- **Filter** für Medientyp, Jahr, Chat und Auffälligkeiten (kein Chat, kein
  Datum, nur Vorschau, Endung ≠ Inhalt) sowie Namenssuche. Die Zahlen sind
  **facettiert**: sie zeigen, was bei dieser Auswahl übrig bliebe, unter
  Berücksichtigung aller anderen aktiven Filter. Optionen, die ins Leere führen,
  sind abgeblendet.
- **Einzelansicht** mit Original, Videoplayer, Metadaten und
  Tastaturnavigation (`←` `→` blättern, `Leertaste` auswählen, `Esc` schließen)
- **Auswahl** über ein Häkchen je Kachel, Shift für einen Bereich und „alle im
  Filter auswählen" für den aktiven Filter. Übernommen wird, was du siehst: die
  Auswahl *innerhalb des aktuellen Filters*. Was du vorher ausgewählt hast und
  was jetzt verdeckt ist, bleibt ausgewählt — die Leiste sagt, wie viel —, ist
  aber nicht Teil der Befehle, bis du den Filter wieder weitest. Die Zahl auf
  dem Schirm stimmt damit immer mit den Häkchen überein.

Drei bewusste Entscheidungen:

**Vorschauen sind keine eigenen Einträge.** Eine Vorschau ist die Kachel ihres
Originals — sonst erschiene jedes Bild doppelt. Eine Vorschau *ohne* Original
bleibt ein eigener Eintrag, markiert als „nur Vorschau": oft ist sie alles, was
von einer gelöschten Datei übrig ist.

**Kacheln wählen die passende Auflösung.** Die Vorschauen der Messenger sind
winzig — bei WhatsApp etwa 100 × 73 px. In einer 200 CSS-px breiten Kachel
auf einem Retina-Display ist das vierfach hochskaliert. Deshalb trägt das
Manifest die echten Pixelmaße, gelesen aus dem Dateikopf, und die Seite bietet
je Kachel beide Auflösungen über `srcset` an. Der Browser wählt die kleinste
ausreichende Quelle.

**Auf der Zeitleiste stehen nur Nachrichtendaten.** Für Dateien, die nur einen
Dateisystem-Zeitstempel aus dem Backup tragen — Einstellungen, Logs, die
Datenbanken —, wäre dieses Datum irreführend: es ist für alle derselbe Tag und
würde die neuesten echten Medien von oben verdrängen. Sie erscheinen unter „kein
Datum"; die Einzelansicht zeigt ihr Dateidatum getrennt.

### Eine Auswahl herausholen

Der Auswahldialog führt auf:

Bei einer kleinen Auswahl ist es ein Befehl mit den Pfaden darin:

```bash
msgx collect \
    --output "~/messenger-extract/export/threema" \
    --target ~/auswahl \
    'media/images/a.jpg' 'media/videos/b.mp4'
```

Bei einer großen verteilt der Dialog sie auf mehrere Befehle — `collect` darf
mehrfach in dasselbe Ziel laufen und merkt sich, was dort schon liegt. Erst wenn
selbst das unzumutbar viele werden, kommt die Liste über die Standardeingabe:

```bash
pbpaste | msgx collect \
    --output "~/messenger-extract/export/threema" \
    --target ~/auswahl \
    --selection -
```

Was auch immer es ist: der Dialog zeigt **jeden** Befehl, den du ausführen
musst, nummeriert.

Unter Windows lautet der Befehl für die Zwischenablage
`powershell -Command Get-Clipboard`; der Dialog nennt den passenden für das
jeweilige System. `--selection DATEI` nimmt auch eine Textdatei mit einem
relativen Pfad je Zeile.

| Option | Wirkung |
|---|---|
| `--no-hardlinks` | echte Kopien — nötig, wenn die Auswahl weitergegeben werden soll |
| `--keep-structure` | die Verzeichnisstruktur des Exports erhalten |
| `--verify` | für jede Datei den SHA-256 gegen das Manifest prüfen |
| `--dry-run` | nur zeigen, was eingesammelt würde |

#### Warum der Browser das nicht kann

Nicht einmal für eine einzelne Datei, und nicht allein wegen der
Same-Origin-Regel. Gemessen:

```
fetch:                BLOCKIERT (TypeError: Failed to fetch)
XMLHttpRequest:       BLOCKIERT
<img> anzeigen:       OK
canvas auslesen:      BLOCKIERT (SecurityError — tainted canvas)
<a download> klicken: ES PASSIERT NICHTS — kein Download, kein Fehler
```

Die letzte Zeile entscheidet, und sie ist nur mit einer Gegenprobe etwas wert.
Dieselbe Seite, derselbe Link, dasselbe `download`-Attribut, dieselbe echte
Nutzergeste, ein ausdrücklich erlaubter Zielordner:

| Ausgeliefert über | Download-Ereignisse | Ergebnis |
|---|---|---|
| `http://` | `downloadWillBegin`, dann Fortschritt | die Datei kommt an |
| `file://` | **überhaupt keine** | nichts, und kein Fehler |

Es ist also das URL-Schema, nicht die Geste und nicht die Einstellung. Chrome
lehnt stillschweigend ab, aus einer als Datei geöffneten Seite irgendetwas
herunterzuladen.

Was weiterhin geht, weil es eine Funktion des Browsers ist und nicht der Seite:
**Rechtsklick auf das Bild → „Bild speichern unter…"**. Für eine einzelne Datei
ist das der kurze Weg, und der Auswahldialog sagt das inzwischen auch.

Richtig beheben ließe es sich nur, indem der Export über `http://localhost`
ausgeliefert wird — und das heißt, einen Socket zu öffnen. Dieses Projekt sagt
zu, dass kein Modul einen importiert, und ein Test erzwingt das. Die
Bequemlichkeit ist die eine Zusage nicht wert, auf der das Ganze steht. Also
wählt das UI aus, und die CLI sammelt ein.

### Prüfen

```bash
msgx verify --manifest "~/messenger-extract/export/threema"
```

Prüft Vorhandensein, Größe und SHA-256 jeder exportierten Datei sowie die
Hardlinks. Das Backup wird **nicht** gebraucht, das funktioniert also auch auf
einer Kopie des Exports, Jahre später, auf einem anderen Rechner.

### Datenbankschemata ansehen

```bash
msgx database --backup "~/messenger-extract/backup/<UDID>"
```

Gibt Tabellen, Spalten, Typen, Primär- und Fremdschlüssel der App-Datenbanken
aus — **keine Inhalte**.

---

## Verschlüsselte Backups

Der Regelfall. Das Werkzeug

1. liest den Keybag aus `Manifest.plist`,
2. leitet den Passcode-Schlüssel mit PBKDF2 ab (doppelt, erst SHA-1, dann
   SHA-256, ab Keybag-Version 3),
3. entpackt die Klassenschlüssel mit AES-KeyWrap (RFC 3394),
4. entschlüsselt `Manifest.db` mit AES-256-CBC in ein temporäres Verzeichnis
   **außerhalb** des Backups,
5. entschlüsselt Nutzdateien bei Bedarf — für die Typerkennung nur deren erste
   Bytes.

### Welches Passwort?

Das im Finder gesetzte, als „Lokales Backup verschlüsseln" aktiviert wurde.
**Nicht** der iPhone-Code und **nicht** das Apple-ID-Passwort. Ein falsches
Passwort scheitert an der Integritätsprüfung des AES-KeyWrap und wird als solches
gemeldet; es entsteht kein Datenmüll.

### Wie danach gefragt wird

Nur interaktiv über `getpass.getpass()`. Es gibt **keine** Kommandozeilenoption,
**keine** Umgebungsvariable und **keine** Konfigurationsdatei — ein Passwort in
`argv` landet in der Shell-Historie und in der Prozessliste. Ein Test prüft jeden
Unterbefehl und belegt, dass keine solche Option existiert.

Eine ehrliche Grenze: CPython kann das sichere Löschen unveränderlicher `str`-
und `bytes`-Objekte nicht garantieren. Abgeleitete Schlüssel liegen in einem
überschreibbaren Puffer, der nach Gebrauch genullt wird; das Passwort selbst kann
bis zum Prozessende im Heap verbleiben. Das verkleinert das Fenster, es schließt
es nicht.

`--metadata-only` erzeugt einen **Teilbericht** aus den unverschlüsselten
Plists: Gerät, iOS-Version, Verschlüsselungszustand und die erkannten Messenger.
Datei- und Medienstatistiken fehlen und werden als fehlend gemeldet, nicht als
Null.

---

## Warum du das selbst nachprüfen kannst

Diesen Abschnitt kann man überspringen. Er ist für den Leser, der wissen will,
warum die Zusagen weiter oben etwas wert sind — schließlich kann ein Werkzeug,
das ein Nachrichtenarchiv anfasst, behaupten was es will.

Zu jeder Zusage unten gibt es einen Test, der fehlschlägt, wenn sie nicht mehr
stimmt. Die Tests liegen im Quellcode, das ist also nachprüfbar, ohne jemandem
glauben zu müssen: `python -m pytest`.

| Zusage | Wie sie nachgewiesen wird |
|---|---|
| Das Backup wird nicht verändert | Ein Fingerabdruck jeder Datei — Inhalt, Größe, Änderungszeit — wird vor und nach einem vollständigen Durchlauf genommen und verglichen. Gegentests belegen, dass der Fingerabdruck Änderungen tatsächlich erkennt. |
| Kein Netzzugriff | Kein Quellmodul importiert `socket`, `urllib`, `http` oder Ähnliches. Statisch geprüft und zusätzlich dynamisch mit abgeschaltetem `socket`. |
| Exportierte Dateien sind unversehrt | Der Quell-SHA-256 wird aus denselben Bytes gebildet, die geschrieben werden; der Ziel-Hash wird danach zurückgelesen. Erst der Vergleich ist der Nachweis. |
| Kein Passwort in Log oder Argumenten | Es gibt keine Passwort-Option. Ein Test prüft jeden Unterbefehl, damit das so bleibt. |
| Es wird nichts geraten | Strukturen werden zur Laufzeit gemessen. Was sich nicht feststellen lässt, führt zu einem Diagnosebericht statt zu einem Ergebnis. |

Von Anfang bis Ende an einem echten iPhone-Backup mit beiden Messengern geprüft:
jede exportierte Datei mit übereinstimmendem SHA-256, kein Schreibfehler, kein
Integritätsfehler. Über 90 % der Medien ließen sich einem Chat zuordnen, der
Rest ging nach `unassigned/` statt geraten zu werden.

Die Messungen dazu stehen im Designdokument. Konkrete Zahlen — wie viele
Dateien, wie groß, wie lange — fehlen hier absichtlich: sie beschreiben das
Gerät und das Nachrichtenvolumen des Autors, nicht das Werkzeug.

---

## Abhängigkeiten

| Paket | Version | Lizenz | Zweck |
|---|---|---|---|
| [`cryptography`](https://github.com/pyca/cryptography) | >=42,<46 | Apache-2.0 OR BSD-3-Clause | PBKDF2, AES-256-CBC, AES-KeyWrap (RFC 3394) |
| `cffi` (transitiv) | — | MIT-0 | Bindings für `cryptography` |
| `pycparser` (transitiv) | — | BSD-3-Clause | Abhängigkeit von `cffi` |
| `pytest`, `pytest-cov`, `ruff` (nur Entwicklung) | — | MIT | Tests und Linting |

Alles andere ist die Python-Standardbibliothek. **Hier wird kein
kryptografisches Primitiv selbst implementiert** — eigener Code beschränkt sich
auf das Parsen von Formaten (TLV-Keybag, NSKeyedArchiver-Plists,
JPEG/PNG/GIF/WEBP-Köpfe).

Zur Laufzeit wird nichts heruntergeladen.

---

## Tests

```bash
~/.venvs/msgbackup-extractor/bin/python -m pytest
~/.venvs/msgbackup-extractor/bin/ruff check src tests
```

704 Tests. Sie brauchen **nie** echte private Daten: jedes Backup wird
synthetisch erzeugt, mit echtem TLV-Keybag, echtem PBKDF2, echtem AES-KeyWrap,
echtem AES-256-CBC und echten NSKeyedArchiver-MBFile-Blobs — sie prüfen also
tatsächlich den Produktionscode und nicht ein vereinfachtes Testformat.

Vier verdienen eine Erwähnung:

- `test_readonly_guarantee.py` nimmt einen Fingerabdruck des gesamten Backups,
  lässt alles laufen und vergleicht erneut. Gegentests belegen, dass der
  Fingerabdruck Änderungen erkennt.
- `test_no_network.py` prüft statisch, dass keine Quelldatei ein Netzmodul
  importiert, und dynamisch, dass sich das gesamte Paket mit abgeschaltetem
  `socket` importieren lässt.
- `test_analysis.py::test_encrypted_and_plain_reports_agree` analysiert dasselbe
  Backup verschlüsselt und unverschlüsselt und vergleicht die Berichte.
- `test_platforms.py` täuscht das Betriebssystem vor, damit die Windows-Pfade
  geprüft und nicht bloß behauptet werden.

Vor einem Push läuft eine zweite Prüfung:

```bash
scripts/check-sensitive.sh
```

Sie durchsucht den Arbeitsbaum **und die vollständige Commit-Historie** nach
persönlichen Daten, rechnerspezifischen Pfaden, Gerätekennungen und Resten einer
Historien-Umschreibung und endet bei einem Treffer mit einem Fehlercode.
Bekannte Fehlalarme — synthetische Fixture-Daten — stehen mit Begründung in
`scripts/sensitive-allowlist.txt`, statt ein Muster zu verwässern.

---

## Fehlerbehebung

**`Kein Leserecht auf …` / `PermissionError` (macOS)** — das Terminal braucht
Festplattenvollzugriff, und es muss danach **neu gestartet** werden.

**`Keine Backups gefunden` (Windows)** — die Meldung nennt die durchsuchten
Pfade und ihren Zustand. Häufige Ursachen: das Backup liegt in iCloud statt
lokal, oder es wurde auf ein anderes Laufwerk verschoben. Der vollständige Pfad
über `--backup` funktioniert weiterhin.

**`Das Passwort ist falsch`** — es ist das Finder-Backup-Passwort, nicht der
Gerätecode und nicht das Apple-ID-Passwort.

**`… sieht nicht wie ein Apple-Backup aus`** — `--backup` muss auf das
Verzeichnis mit der Gerätekennung zeigen, nicht auf `MobileSync/Backup` selbst.
`msgx backups` listet die Kandidaten.

**`Diagnosebericht: keine Dateitabelle gefunden`** — das Backup hat eine
Struktur, die dieses Werkzeug nicht kennt. Es hält bewusst an, statt zu raten;
der Bericht listet die tatsächlich vorhandenen Tabellen und Spalten und ist die
brauchbare Grundlage für eine Fehlermeldung.

**`import msgbackup_extractor` scheitert** — liegt das venv in iCloud Drive?
Siehe [Installation](#macos).

**`pbpaste: command not found` (Windows)** — das ist ein macOS-Befehl; unter
Windows heißt er `powershell -Command Get-Clipboard`.

---

## Bekannte Einschränkungen

1. Extrahieren lässt sich nur, was im Backup tatsächlich **vorhanden** ist. Apps
   können Dateien ausschließen — Signal schließt alles aus.
2. Schutzklassen, die allein am Geräteschlüssel hängen, lassen sich aus einem
   Backup nicht öffnen. Betroffene Dateien werden als nicht entschlüsselbar
   gezählt, nicht stillschweigend übersprungen.
3. Ein Manifest-Eintrag mit unlesbarem MBFile-Blob hat keinen Dateischlüssel. In
   einem verschlüsselten Backup ist sein Inhalt damit Geheimtext und sein Typ
   nicht bestimmbar; er wird als nicht entschlüsselbar gemeldet, statt aus
   Geheimtext einen Typ zu erfinden.
4. Eine im Backup abgeschnittene Datei, deren Länge zufällig ein Vielfaches von
   16 Byte ist, entschlüsselt fehlerfrei — nur zu weniger Daten. Die Zahl der
   entschlüsselten Bytes wird deshalb immer gegen die im Manifest vermerkte Größe
   geprüft.
5. Core Data stellt jedem Blob ein Markierungsbyte voran (`0x01` inline, `0x02`
   Verweis). Es wird entfernt; ein unerwarteter Wert wird gemeldet und nicht
   blind abgeschnitten.
6. Für viele WhatsApp-Einträge liegt im Backup **nur** das Vorschaubild (meist
   um 100 px breit). Diese Kacheln können nicht scharf sein; die Einzelansicht
   sagt das.
7. Nachrichtentexte werden nicht exportiert. Das UI kann sie daher nicht zeigen.
8. Das sichere Löschen des Passworts aus dem Python-Heap ist nicht garantierbar.
9. Der Cloud-Wächter erkennt die üblichen Orte über den Pfad. Ein beliebig
   konfigurierter Sync-Ordner liegt außerhalb seiner Reichweite.
10. **Windows ist implementiert, aber ungeprüft.** Siehe
    [Voraussetzungen](#windows-ist-ungeprüft).
11. Für andere Systeme (Linux, BSD) ist kein Standardort bekannt, und es wird
    keiner erfunden. Ein kopiertes Backup funktioniert weiterhin über
    `--backup`.

Messenger können ihre interne Struktur zwischen Versionen ändern. Strukturen
werden zur Laufzeit erkannt, aber bei einer unbekannten Struktur kann das
Werkzeug nur einen Diagnosebericht erzeugen.

---

## Bestimmungsgemäße Verwendung

> **Wichtig** — Dieses Werkzeug ist für das **eigene** Backup auf dem **eigenen**
> Rechner.

Backups enthalten Nachrichten, die mit anderen Menschen gewechselt wurden. Die
rein private Nutzung fällt unter die Haushaltsausnahme der DSGVO (Art. 2 Abs. 2
lit. c); **das Veröffentlichen oder Weitergeben extrahierter Daten nicht — ab
dann sind Sie dafür verantwortlich.**

> **Achtung, Strafbarkeit** — Es auf das Backup einer anderen Person anzuwenden,
> kann ohne Befugnis eine **Straftat** sein, in Deutschland etwa nach § 202a
> StGB (Ausspähen von Daten). Tun Sie das nicht.

Threema, WhatsApp und Signal sind Marken der jeweiligen Inhaber und werden hier
nur genannt, um zu beschreiben, welche Formate gelesen werden können. Dieses
Projekt steht in keiner Verbindung zu ihnen und wird von ihnen weder unterstützt
noch gefördert. Siehe [`NOTICE`](NOTICE).

---

## Lizenz

[Apache License 2.0](LICENSE). Copyright 2026 Markus Mueller.

Apache-2.0 statt MIT aus zwei Gründen, die hier greifen: die Lizenz enthält
einen ausdrücklichen Gewährleistungsausschluss und eine Haftungsbegrenzung, und
sie verlangt, geänderte Dateien als geändert zu kennzeichnen. Wer dieses Projekt
forkt und die Read-only-Wächter oder die Integritätsprüfungen entfernt, macht
die Änderung damit sichtbar.
