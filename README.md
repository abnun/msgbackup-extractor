# Messenger Backup Extractor

Lokales Kommandozeilenwerkzeug für macOS, das Messenger-Daten aus einem lokalen
Apple-iPhone-Backup identifiziert und in eine normale Dateistruktur exportiert.

Erster unterstützter Messenger ist **Threema**; die App-Erkennung ist als Plugin
gebaut, WhatsApp und Signal sind vorgesehen.

> **Status:** in Entwicklung.
>
> | Befehl | Stand |
> |---|---|
> | `analyze` | fertig, auch für verschlüsselte Backups |
> | `database` | fertig, auch für verschlüsselte Backups |
> | `backups` | fertig |
> | `extract` | in Arbeit |
> | `verify` | in Arbeit |
>
> Danach vorgesehen: Chat-Zuordnung (`--organize-by-chat`), ein zweites
> App-Profil (WhatsApp/Signal) und ein lokales UI zum Durchsehen des Exports.
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

## Voraussetzungen

- macOS
- Python 3.12 oder neuer
- ein lokales Finder-Backup des iPhones
- „Festplattenvollzugriff" für das Terminal, um
  `~/Library/Application Support/MobileSync/Backup/` lesen zu können
  (Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff)

## Installation

```bash
python3.12 -m venv ~/.venvs/msgbackup-extractor
~/.venvs/msgbackup-extractor/bin/pip install -e ".[dev]"
```

### Wichtig: venv nicht in iCloud Drive anlegen

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

Das Programm bricht ab, wenn `--output` in einem Cloud-Sync-Container liegt
(iCloud Drive, Dropbox, OneDrive, Google Drive). Sonst würde macOS die
extrahierten Daten selbsttätig hochladen.

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

**`Kein Leserecht auf …` / `PermissionError`**
Das Terminal braucht „Festplattenvollzugriff": Systemeinstellungen →
Datenschutz & Sicherheit → Festplattenvollzugriff → Terminal hinzufügen und
Terminal neu starten.

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
5. Threema kann Teile seiner Daten zusätzlich app-eigen verschlüsseln. Ob und
   wie weit das eine Rolle spielt, ist erst nach der Analyse eines echten
   Backups beurteilbar und wird dann hier dokumentiert — nicht vorab versprochen.
6. Threema kann seine interne Struktur zwischen Versionen ändern. Das Programm
   erkennt Strukturen dynamisch, kann bei unbekannten Strukturen aber nur einen
   Diagnosebericht liefern statt Ergebnisse.
7. Sicheres Löschen des Passworts aus dem Python-Heap ist nicht garantierbar
   (siehe oben).
8. Der Cloud-Sync-Guard erkennt die üblichen Ablagen anhand des Pfads. Einen
   beliebig konfigurierten Sync-Ordner kann er nicht kennen.

Weitere Details: §17 des Design-Dokuments.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
