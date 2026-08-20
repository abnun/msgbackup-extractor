# Messenger Backup Extractor

Lokales Kommandozeilenwerkzeug für macOS, das Messenger-Daten aus einem lokalen
Apple-iPhone-Backup identifiziert und in eine normale Dateistruktur exportiert.

Erster unterstützter Messenger ist **Threema**; die App-Erkennung ist als Plugin
gebaut, WhatsApp und Signal sind vorgesehen.

> **Status:** in Entwicklung. `analyze` und `extract` sind noch nicht fertig.
> Diese README wird mit dem Funktionsumfang mitgeführt.

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
echter PBKDF2-Ableitung, echtem AES-Key-Wrap und echter AES-256-CBC-
Verschlüsselung, damit sie den Produktionscode tatsächlich auf die Probe stellen.

## Bekannte Einschränkungen

Dieses Programm kann nur Daten extrahieren, die tatsächlich im zugänglichen
Apple-Backup vorhanden sind. Es garantiert nicht, dass jede historische
Messenger-Datei vorhanden oder entschlüsselbar ist.

Weitere Einschränkungen: siehe §17 des Design-Dokuments.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
