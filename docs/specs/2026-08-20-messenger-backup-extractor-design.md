# Design: Messenger Backup Extractor (macOS, lokal)

**Datum:** 2026-08-20
**Status:** freigegeben
**Erster Ziel-Messenger:** Threema (iOS)
**Geplant:** WhatsApp, Signal

---

## 1. Zweck und Abgrenzung

Ein vollständig lokales Open-Source-Kommandozeilenwerkzeug für macOS, das aus einem
lokalen Apple-Finder-Backup eines iPhones die Daten eines Messengers identifiziert
und in eine normale Dateistruktur exportiert.

**Im Umfang:**

- Analyse eines lokalen Apple-Backups (verschlüsselt und unverschlüsselt)
- Dynamische Erkennung der installierten Messenger-App über Bundle Identifier
- Identifikation von Chat-Datenbanken, Bildern, Videos, Audio, Dokumenten, Metadaten
- Export in eine strukturierte Ausgabe mit SHA-256-Integritätsprüfung
- Optionale Zuordnung von Medien zu Chats anhand der App-Datenbank
- Schema-Introspektion statt fest verdrahteter Annahmen

**Nicht im Umfang:**

- Zugriff auf das iPhone selbst (kein USB-Protokoll, kein Jailbreak)
- Entschlüsselung von Threemas eigener DB-Verschlüsselung, falls vorhanden und
  ohne im Backup enthaltenes Schlüsselmaterial nicht möglich (siehe § 11)
- Rekonstruktion von Daten, die nicht im Backup enthalten sind
- Jede Form von Netzwerkkommunikation

**Kernaussage für die README:** Dieses Programm kann nur Daten extrahieren, die
tatsächlich im zugänglichen Apple-Backup vorhanden sind. Es garantiert nicht, dass
jede historische Messenger-Datei vorhanden oder entschlüsselbar ist.

---

## 2. Nicht verhandelbare Anforderungen

| # | Anforderung | Umsetzung |
|---|---|---|
| R1 | Ausschließlich lokal | Keine Netzwerk-Imports; Test erzwingt das |
| R2 | Keine Datenübertragung nach außen | s. R1 + Cloud-Sync-Guard auf Pfaden |
| R3 | Keine Cloud-Dienste | Guard verweigert Pfade in iCloud/Dropbox/OneDrive |
| R4 | Keine Telemetrie/Analytics | Nicht vorhanden |
| R5 | Kein HTTP/HTTPS zur Laufzeit | s. R1 |
| R6 | Keine Laufzeit-Downloads | Alle Dependencies zur Installationszeit, gepinnt |
| R7 | Backup wird nie verändert | Read-only-Architektur, § 6 |
| R8 | Schreiben nur nach `--output` | Output-Guard, § 6 |
| R9 | Passwort nie als CLI-Arg/Log/Datei | Nur `getpass`, § 7 |
| R10 | Keine Nachrichteninhalte im Log | Redaction-Filter, § 8 |
| R11 | Nicht raten | Verifizierte Erkennung + Diagnosebericht, § 5 |

---

## 3. Das Apple-Backup-Format

Analyse-Ergebnis; Grundlage der Implementierung.

### 3.1 Verzeichnisstruktur

```
<UDID>/
├── Info.plist        # unverschlüsselt, auch bei verschlüsseltem Backup
├── Manifest.plist    # unverschlüsselt; enthält Keybag und App-Liste
├── Status.plist      # unverschlüsselt
├── Manifest.db       # SQLite; bei verschlüsseltem Backup selbst verschlüsselt
├── 00/ 01/ ... ff/   # Nutzdaten, Dateiname = fileID (40 Hex)
```

`fileID = SHA1("<domain>-<relativePath>")`. Die ersten zwei Hex-Zeichen bestimmen
das Unterverzeichnis.

### 3.2 `Manifest.db`, Tabelle `Files`

Typische Struktur:

```sql
CREATE TABLE Files (
    fileID       TEXT PRIMARY KEY,
    domain       TEXT,
    relativePath TEXT,
    flags        INTEGER,
    file         BLOB
);
```

**Wichtig:** Es gibt *keine* Spalten `size`, `protectionClass`, `encryptionKey`,
`mode`. Diese Felder stecken in der Spalte `file` als binäres
**NSKeyedArchiver**-Plist (ein `MBFile`-Objekt). Daraus zu dekodieren:

`Size`, `ProtectionClass`, `EncryptionKey`, `Mode`, `UserID`, `GroupID`,
`InodeNumber`, `Birth`, `LastModified`, `LastStatusChange`, `RelativePath`, `Flags`.

Bedeutung von `Files.flags`: `1` = Datei, `2` = Verzeichnis, `4` = Symlink.

**Kein blindes Vertrauen auf dieses Schema.** Die Implementierung führt
`PRAGMA table_info(Files)` bzw. eine Tabellensuche aus, arbeitet mit den
tatsächlich vorhandenen Spalten und erzeugt bei unbekannter Struktur einen
Diagnosebericht statt falscher Ergebnisse.

### 3.3 `Manifest.plist`

Relevante Schlüssel:

- `IsEncrypted` (bool)
- `BackupKeyBag` (Data) — TLV-kodierter Keybag
- `ManifestKey` (Data) — gewrappter Schlüssel für `Manifest.db` (ab iOS 10.2)
- `Applications` (Dict): Bundle Identifier → `CFBundleIdentifier`,
  `CFBundleVersion`, `iTunesMetadata`, `PlaceholderIcon`
- `Version`, `Date`, `WasPasscodeSet`, `Lockdown`, `SystemDomainsVersion`

### 3.4 `Info.plist`

`Installed Applications` (Liste von Bundle-IDs), `Device Name`, `Product Version`,
`Product Type`, `Last Backup Date`, `Serial Number`, `IMEI`, `Unique Identifier`.

**Konsequenz für `analyze`:** Bundle-IDs sind ohne Passwort lesbar. Ein
Teilbericht (Gerät, iOS-Version, verschlüsselt ja/nein, Messenger erkannt,
App-Version) funktioniert passwortfrei. Datei- und Medienstatistiken benötigen
`Manifest.db` und damit bei verschlüsseltem Backup das Passwort.

### 3.5 `Status.plist`

`IsFullBackup`, `BackupState`, `Date`, `SnapshotState`, `UUID`, `Version`.

---

## 4. Verschlüsselte Backups

### 4.1 Ablauf

1. **Keybag parsen.** `BackupKeyBag` ist TLV (4-Byte-ASCII-Tag, 4-Byte-Länge
   big-endian, Value). Header-Tags: `VERS`, `TYPE`, `UUID`, `HMCK`, `WRAP`,
   `SALT`, `ITER`, `DPSL`, `DPIC`. Danach je Protection Class ein Block:
   `UUID`, `CLAS`, `WRAP`, `KTYP`, `WPKY`.

2. **Passcode-Key ableiten.** Keybag-Version >= 3 (doppeltes PBKDF2):

   ```
   inner = PBKDF2-HMAC-SHA1  (password, DPSL, DPIC, dkLen=32)
   key   = PBKDF2-HMAC-SHA256(inner,    SALT, ITER, dkLen=32)
   ```

   Ältere Keybags: einfaches `PBKDF2-HMAC-SHA1(password, SALT, ITER, 32)`.

3. **Klassenschlüssel entpacken.** `WPKY` je Klasse mit **AES-Key-Wrap
   (RFC 3394)** und dem Passcode-Key unwrappen. Ein falsches Passwort lässt die
   eingebaute Integritätsprüfung des Key-Wrap fehlschlagen — das ergibt einen
   eindeutigen, sauberen Fehler „falsches Passwort" statt Datenmüll.

4. **`Manifest.db` entschlüsseln.** `ManifestKey` = 4 Byte Protection Class +
   40 Byte gewrappter Schlüssel. Unwrappen, dann **AES-256-CBC mit Null-IV**.
   Ergebnis in eine temporäre Datei *außerhalb* des Backups.

5. **Pro Nutzdatei.** `EncryptionKey` aus dem MBFile-Blob (ebenfalls 4 Byte
   Klasse + 40 Byte Wrapped Key), unwrappen, AES-256-CBC/Null-IV entschlüsseln,
   auf `Size` kürzen.

### 4.2 Nicht entschlüsselbare Dateien

Protection Classes, deren Schlüssel im Keybag nicht verfügbar sind, werden im
Bericht **explizit als „nicht entschlüsselbar" gezählt und begründet**, nicht
stillschweigend übersprungen.

---

## 5. Messenger-Erkennung (Plugin-Architektur)

Threema ist der erste, aber nicht der einzige Ziel-Messenger. Die App-Erkennung
ist daher von Anfang an ein Plugin und kein Sonderfall.

```python
class AppProfile(Protocol):
    name: str
    def candidate_bundle_ids(self) -> list[str]: ...
    def detect(self, backup: BackupInfo) -> DetectionResult: ...
    def domains(self, detection: DetectionResult) -> list[DomainMatch]: ...
    def classify_databases(self, dbs: list[DatabaseCandidate]) -> list[DatabaseRole]: ...
    def link_media(self, db: Connection, media: list[MediaFile]) -> list[ChatAssignment]: ...
```

`candidate_bundle_ids()` liefert *Kandidaten zur Verifikation*, nicht zum Raten.
Ein Kandidat gilt erst als erkannt, wenn er in `Info.plist:Installed Applications`
oder `Manifest.plist:Applications` tatsächlich auftaucht.

**Entscheidungslogik in `detect()`:**

| Situation | Verhalten |
|---|---|
| Genau ein Kandidat verifiziert | Erkannt, Bericht nennt Bundle-ID und Version |
| Mehrere Threema-relevante Domains | Alle getrennt anzeigen, keine Auswahl erraten |
| Kandidat plausibel, aber unbestätigt | **Diagnosebericht, Analyse stoppen** |
| Kein Kandidat gefunden | Klare Meldung + Liste aller gefundenen Bundle-IDs |

Domains im Backup folgen dem Muster `AppDomain-<bundle-id>`,
`AppDomainGroup-<group-id>`, `AppDomainPlugin-<extension-id>`. Alle drei werden
berücksichtigt und getrennt ausgewiesen — App-Group-Container sind bei Threema
für Share-Extension-Medien relevant.

---

## 6. Read-only-Garantie und Output-Guard

Fünf Schichten, absichtlich redundant:

1. **Kapselung.** Nur `core/backup.py` kennt den Backup-Pfad. Alle Lesezugriffe
   laufen durch dessen API, geöffnet ausschließlich mit `open(..., "rb")`.

2. **SQLite immutable.** Verbindungen immer als
   `sqlite3.connect("file:<pfad>?mode=ro&immutable=1", uri=True)`.
   `immutable=1` ist entscheidend: es verhindert, dass SQLite `-wal`, `-shm`
   oder Journal-Dateien neben der Originaldatenbank anlegt. Ohne dieses Flag
   wäre „read-only" faktisch verletzt.

3. **Temp-Dateien außerhalb.** Entschlüsselte `Manifest.db` und alle
   Zwischenergebnisse landen in einem `tempfile.TemporaryDirectory()`, dessen
   Basis über `--output` bzw. `TMPDIR` gesteuert wird, niemals im Backup-Ordner.

4. **Output-Guard.** Vor jedem Schreibvorgang prüft `core/paths.py`, dass der
   nach `Path.resolve()` aufgelöste Zielpfad *innerhalb* von `--output` und
   *nicht* innerhalb des Backup-Pfads liegt. Verletzung = Abbruch mit Fehler.

5. **Cloud-Sync-Guard.** `--backup` und `--output` werden gegen bekannte
   Sync-Container geprüft (`~/Library/Mobile Documents/`, `~/Dropbox`,
   `~/OneDrive*`, `~/Google Drive*`, `~/pCloud*`). Treffer bei `--output`
   bedeutet Abbruch, überschreibbar nur mit explizitem `--allow-cloud-output`.
   Begründung: sonst lädt macOS die extrahierten Daten selbsttätig hoch und
   R2/R3 sind verletzt, ohne dass das Programm etwas falsch gemacht hätte.

**Nachweis im Test:** Ein Test hasht jede Datei eines synthetischen Backups samt
`mtime`, führt einen vollständigen `extract`-Lauf aus und vergleicht danach
erneut. Jede Abweichung lässt den Test fehlschlagen.

---

## 7. Passwortbehandlung

- Eingabe ausschließlich über `getpass.getpass("Password: ")`.
- **Kein** CLI-Flag, **keine** Environment-Variable, **keine** Config-Datei,
  **kein** `.env`, **kein** Keychain-Write.
- Das Passwort wird direkt in die Schlüsselableitung gegeben; die Referenz wird
  danach fallengelassen.
- Abgeleitete Klassenschlüssel liegen in einem `SecretBytes`-Objekt mit
  explizitem `wipe()` (Überschreiben eines `bytearray`) und Context-Manager.
- **Ehrliche Grenze, die in der README dokumentiert wird:** CPython garantiert
  für unveränderliche `str`/`bytes` kein sicheres Löschen. Das Passwort kann als
  String-Objekt im Heap verbleiben, bis der Interpreter endet. `wipe()` reduziert
  das Fenster, beseitigt es aber nicht. Wer stärkere Garantien braucht, muss den
  Prozess kurz halten und Swap verschlüsselt betreiben (macOS: Standard).

---

## 8. Logging und Datenschutz

Der Logger erhält einen **Redaction-Filter**, der strukturell verhindert, dass
Inhalte durchrutschen:

**Erlaubt im Log:** Zähler, `fileID`, `domain`, Medientyp, Dateigröße,
Fehlerklasse, Phasenname, Zeitmessung.

**Verboten, auch bei `--verbose`:** Nachrichtentexte, Kontaktnamen,
Telefonnummern, Threema-IDs, Datenbankinhalte, Passwörter, vollständige
`relativePath`-Werte mit potenziellen Klarnamen.

Klartextpfade erscheinen nur mit dem expliziten Flag `--show-paths`, und auch
dann nicht in Dateilogs. Der Analysebericht ist standardmäßig aggregiert.

---

## 9. Architektur

```
src/msgbackup_extractor/
├── __init__.py
├── cli.py                  # argparse-Subcommands, keine Businesslogik
├── models.py               # dataclasses (frozen, wo möglich)
├── core/
│   ├── backup.py           # AppleBackup: Besitzer des Pfads, read-only Zugriff
│   ├── keybag.py           # TLV-Keybag-Parsing (reines Format-Parsing)
│   ├── encryption.py       # Key-Ableitung + Datei-Entschlüsselung
│   ├── manifest.py         # Manifest.db: Schema-Introspektion, MBFile-Dekodierung
│   ├── sqlite_ro.py        # read-only/immutable Connections + Schema-Dump
│   ├── media.py            # Magic Bytes + MIME + Endung -> Medienklasse
│   ├── hashing.py          # streamendes SHA-256
│   ├── paths.py            # Sanitisierung, Output-Guard, Cloud-Guard, Namensschema
│   ├── logging_setup.py    # Redaction-Filter
│   ├── secrets.py          # SecretBytes mit wipe()
│   └── reports.py          # Analyse-/Extraktions-/Diagnoseberichte
├── apps/
│   ├── base.py             # AppProfile-Protokoll
│   ├── registry.py         # Auto-Discovery der Profile
│   ├── threema.py          # Phase 1
│   ├── whatsapp.py         # später
│   └── signal.py           # später
└── extract/
    ├── planner.py          # baut ExtractionPlan (Basis auch für --dry-run)
    ├── runner.py           # führt aus, fehlertolerant pro Datei
    └── verify.py           # SHA-256-Vergleich, verify-Subcommand
```

**Modulgrenzen:** Jedes Modul hat genau eine Aufgabe und eine schmale
öffentliche API. `keybag.py` kennt keine Dateien, `encryption.py` kennt kein
SQLite, `manifest.py` kennt keine Krypto (es erhält einen bereits
entschlüsselten Pfad), `media.py` kennt kein Backup. `planner.py` ist rein
funktional und dadurch ohne Dateisystem testbar — genau das macht `--dry-run`
korrekt statt zu einer zweiten Codepfad-Variante.

### 9.1 Datenfluss `extract`

```
CLI-Argumente validieren (Cloud-Guard, Output-Guard)
  -> AppleBackup öffnen (read-only)
  -> Info.plist / Manifest.plist lesen
  -> falls verschlüsselt: getpass -> Keybag -> Klassenschlüssel
  -> Manifest.db (ggf. entschlüsselt) in Temp
  -> Schema introspizieren  --(unbekannt)-> Diagnosebericht, Stop
  -> Files-Einträge + MBFile-Blobs lesen
  -> AppProfile.detect()    --(unklar)----> Diagnosebericht, Stop
  -> AppProfile.domains() -> Kandidatendateien
  -> media.classify() je Datei (Magic Bytes)
  -> planner.build() -> ExtractionPlan
  -> --dry-run? -> Bericht ausgeben, Ende
  -> runner.run(): pro Datei entschlüsseln/kopieren, hashen, vergleichen
  -> export-manifest.json + reports/extraction-report.json schreiben
```

### 9.2 Fehlertoleranz

Jede Datei ist eine eigene Transaktion. Eine Exception erzeugt einen
`FailedFile(file_id, reason_class)` im Report und eine `WARNING`-Zeile, dann geht
es weiter. Nur Fehler, die den gesamten Lauf sinnlos machen, brechen ab:
falsches Passwort, `Manifest.db` unlesbar, `--output` nicht beschreibbar,
Messenger nicht eindeutig erkannt.

Abschlussbericht:

```
Extraction completed

Successful:        [Anzahl entfernt]
Failed:                17
Skipped:                0
Undecryptable:          0
Integrity errors:       0

Report: reports/extraction-report.json
```

---

## 10. Medienerkennung

Dreistufig, in dieser Priorität: **Magic Bytes** > **MIME (`mimetypes`)** >
**Dateiendung**. Widersprüche werden im Manifest festgehalten
(`extension_mismatch: true`), nicht stillschweigend aufgelöst.

Mindestens unterstützt:

- **Bild:** JPEG, PNG, HEIC/HEIF, GIF, WEBP
- **Video:** MP4, MOV, M4V
- **Audio:** M4A, AAC, WAV, OPUS, OGG, CAF
- **Dokument:** PDF, TXT, RTF
- **Archiv/Office:** ZIP, DOC, DOCX, XLS, XLSX, PPT, PPTX

HEIC und die MP4-Familie werden über die `ftyp`-Box bei Offset 4 und deren
Brand-Code unterschieden; OOXML-Formate über die ZIP-Signatur plus
`[Content_Types].xml`. Die im Backup **tatsächlich gefundenen** Formate erscheinen
dynamisch im Analysebericht — die Liste oben ist die Erkennungsfähigkeit, nicht
die Berichtsstruktur.

---

## 11. Exportstruktur

```
<output>/
├── media/
│   ├── images/  videos/  audio/  documents/  other/
├── databases/
├── metadata/
├── reports/
│   ├── analysis-report.json
│   └── extraction-report.json
└── export-manifest.json
```

Mit `--organize-by-chat` zusätzlich:

```
<output>/chats/<chat-name>/{images,videos,audio,documents}/
<output>/chats/unassigned/...
```

**Chat-Zuordnung nur bei Belegbarkeit.** Eine Datei wird einem Chat nur dann
zugeordnet, wenn eine explizite Verknüpfung in der App-Datenbank existiert
(Fremdschlüssel oder eindeutige ID-Referenz). Heuristiken über
Zeitstempel-Nähe oder Dateinamensmuster werden **nicht** verwendet. Alles
Unklare landet in `unassigned/`.

Chat-Verzeichnisnamen werden für das Dateisystem saniert und bei Kollision
eindeutig gemacht; das Mapping steht im `export-manifest.json`.

### 11.1 Dateinamen

Originaldateiname bevorzugen, falls die Datenbank oder das Manifest einen
enthält. Sonst:

```
YYYY-MM-DD_HH-MM-SS_<type>_<sha256[:8]>.<ext>     z.B. 2025-03-14_18-42-11_image_a8f31c2e.jpg
unknown-date_<sha256[:8]>.<ext>                    falls kein Datum belegbar
```

Datumsquellen ausschließlich: MBFile-`LastModified`/`Birth` oder ein
Datenbank-Zeitstempel. **Keine erfundenen Metadaten.**

### 11.2 `export-manifest.json`

Pro Datei mindestens:

```json
{
  "source_domain": "AppDomain-…",
  "source_relative_path": "…",
  "file_id": "…",
  "output_path": "media/images/…",
  "size": 123456,
  "sha256": "…",
  "media_type": "image/jpeg",
  "detection_method": "magic",
  "extension_mismatch": false,
  "protection_class": 3,
  "duplicate_of": null,
  "integrity_ok": true
}
```

### 11.3 Duplikate

Identische SHA-256-Werte werden als `duplicate_of` markiert, **nie automatisch
gelöscht**. `--deduplicate` schreibt Duplikate nur einmal aus und verweist im
Manifest auf die behaltene Kopie.

### 11.4 Integritätsprüfung

Der Hash wird über den *entschlüsselten Quellinhalt* im Speicher/Stream gebildet
und mit dem Hash der geschriebenen Zieldatei verglichen. Bei Abweichung:
Eintrag `integrity_ok: false`, `ERROR` im Log, Zähler im Report. Der Lauf bricht
deswegen nicht ab.

---

## 12. CLI

```
msgx analyze  --backup PATH [--app NAME] [--json PATH] [--verbose] [--show-paths]
msgx extract  --backup PATH --output PATH [--app NAME] [--dry-run]
              [--organize-by-chat] [--deduplicate] [--types image,video,…]
msgx database --backup PATH [--app NAME] [--json PATH]
msgx verify   --manifest PATH
msgx --help
```

`--app` ist optional; ohne Angabe werden alle registrierten Profile geprüft und
bei mehreren Treffern die Auswahl verlangt statt geraten. Zusätzlicher Alias-
Entrypoint ist nicht geplant.

---

## 13. Dependencies

| Zweck | Paket | Lizenz | Begründung |
|---|---|---|---|
| Krypto-Primitive | `cryptography` (PyCA) | Apache-2.0 ODER BSD-3 | `PBKDF2HMAC`, AES-CBC, `keywrap.aes_key_unwrap` (RFC 3394). De-facto-Standard, breit auditiert, aktiv gewartet, keine Netzwerkfunktion |
| Alles andere | Standard Library | PSF | `sqlite3`, `plistlib`, `hashlib`, `pathlib`, `argparse`, `getpass`, `logging`, `dataclasses`, `typing`, `mimetypes`, `json`, `tempfile`, `struct` |
| Tests (dev) | `pytest` | MIT | Nur Dev-Dependency, nicht im Runtime-Pfad |

**Keine eigene Kryptografie.** Es wird kein kryptografisches Primitiv selbst
implementiert. Eigener Code beschränkt sich auf Container- und Formatparsing
(TLV-Keybag, NSKeyedArchiver-Plist) — Dateiformat-Logik, keine Krypto.

**Warum keine fertige Backup-Bibliothek.** `iOSbackup` und
`iphone_backup_decrypt` sind funktional geeignet, wurden aber verworfen, weil
(a) deren Lizenzstatus vor Verwendung erst zu verifizieren wäre und der
Präferenz MIT/BSD/Apache möglicherweise widerspricht, (b) beide ein festes
`Manifest.db`-Schema annehmen und die geforderte Introspektion nicht liefern,
(c) `cryptography` ohnehin benötigt wird und damit eine Dependency statt zwei
genügt. `encryption.py` liegt hinter einem schmalen Interface, ein alternatives
Backend bleibt nachrüstbar.

**Installation.** Python 3.12 (Homebrew), `python3.12 -m venv .venv`,
`pip install -e ".[dev]"`, einmalig und bewusst durch den Nutzer.
Zusätzlich ein `requirements.lock` mit Hashes für `pip install --require-hashes`.
Zur **Laufzeit** wird nichts nachgeladen.

---

## 14. Datenlayout auf der Platte

Der Code liegt im iCloud-Projektordner (unkritisch). Die **Daten** liegen lokal
und unsynchronisiert, mit einem Unterverzeichnis pro Messenger:

```
~/messenger-extract/
├── backup/            # das Apple-Backup, gemeinsame Quelle für alle Messenger
│   └── <UDID>/
└── export/
    ├── threema/
    ├── whatsapp/
    └── signal/
```

Der Cloud-Guard aus § 6 setzt das durch.

---

## 15. Teststrategie

Tests benötigen **niemals** echte private Daten. Grundlage ist ein
Fixture-Generator, der synthetische Apple-Backups erzeugt — er ist das erste
gebaute Artefakt, weil alles Weitere darauf aufbaut.

**Fixture-Generator erzeugt:**

- unverschlüsseltes Backup mit bekanntem Inhalt
- verschlüsseltes Backup mit bekanntem Passwort (echter Keybag, echtes AES)
- Backup mit abweichendem `Files`-Schema
- Backup mit unbekanntem/kaputtem Schema
- Einträge, deren Nutzdatei im Dateisystem fehlt
- beschädigte/abgeschnittene Nutzdateien
- Datei mit falscher Endung (Magic-Bytes-Widerspruch)
- `relativePath` mit `../`, absoluten Pfaden, NUL, Unicode-Normalisierungsfallen

**Testfälle:**

| Bereich | Prüfung |
|---|---|
| Manifest | Schema-Introspektion, MBFile-Blob-Dekodierung, Schemavarianten, unbekanntes Schema -> Diagnose |
| Keybag | TLV-Parsing, Ableitung v3 und Legacy, falsches Passwort -> klarer Fehler |
| Entschlüsselung | Manifest.db, Nutzdateien, Größen-Truncation, nicht verfügbare Klasse |
| SQLite | `mode=ro&immutable=1`, keine `-wal`/`-shm`-Datei entsteht |
| Read-only | Hash+mtime des ganzen Backups vor/nach vollem Extract-Lauf identisch |
| Medien | alle Formate aus § 10, Endungs-Widerspruch, unbekannte Signatur |
| Hashing | SHA-256 gegen bekannte Vektoren, Streaming großer Dateien |
| Extraktion | Erfolg, fehlende Datei, beschädigte Datei, Fortsetzung nach Fehler |
| Integrität | manipulierte Zieldatei -> `integrity_ok: false` |
| Dry Run | schreibt nachweislich nichts, Zähler identisch zum echten Lauf |
| Pfade | Traversal-Abwehr, Output-Guard, Cloud-Guard, Namenskollisionen |
| Erkennung | eindeutig, mehrdeutig, nicht gefunden, mehrere Domains |
| Logging | Redaction-Filter lässt keine Inhalte/Passwörter durch, auch verbose |
| Netzwerk | Quellcode enthält keine Netzwerk-Imports; `socket` gepatcht -> kein Aufruf |
| Duplikate | Markierung, `--deduplicate` |

---

## 16. Implementierungsphasen

| Phase | Inhalt | Gate |
|---|---|---|
| 0 | Scaffolding: `pyproject.toml`, `LICENSE`, `models.py`, `logging_setup.py`, `secrets.py`, Fixture-Generator | — |
| 1 | `analyze` für unverschlüsselte Backups: `backup`, `manifest`, `sqlite_ro`, `media`, `paths`, `reports`, `threema`-Detection, CLI | — |
| 2 | Verschlüsselte Backups: `keybag`, `encryption`, Manifest-Entschlüsselung, `getpass`-Flow | — |
| 3 | Vollständige Testsuite für Phase 0–2 | **Nutzer führt `analyze` auf echtem Backup aus und liefert anonymisierten Bericht** |
| 4 | `extract` + `verify`: `planner`, `runner`, `--dry-run`, `--deduplicate`, Export-Manifest, Integrität, Tests | **erledigt** |
| 5 | `database`-Subcommand + `--organize-by-chat` auf Basis des real vorgefundenen Schemas | **erledigt** |
| 6 | README vollständig + zweites App-Profil (WhatsApp oder Signal) zur Validierung des Interfaces | — |
| 7 | Lokales UI zum Durchsehen des Exports (siehe §18) | — |

---

## 17. Bekannte Einschränkungen (für die README)

1. Es können nur Daten extrahiert werden, die im Backup vorhanden sind. Apple
   schließt bestimmte Daten von Backups aus; Apps können Dateien als
   „nicht sichern" markieren.
2. Threema kann Teile seiner Daten zusätzlich app-eigen verschlüsseln. Ob und
   wie weit das eine Rolle spielt, ist erst nach der Analyse des echten Backups
   beurteilbar und wird dann ehrlich dokumentiert — nicht vorab versprochen.
3. Protection Classes ohne verfügbaren Schlüssel bleiben unentschlüsselbar und
   werden als solche gezählt.
4. Sicheres Löschen des Passworts aus dem Python-Heap ist nicht garantierbar
   (§ 7).
5. Threema kann seine interne Struktur zwischen Versionen ändern. Das Programm
   erkennt Strukturen dynamisch, kann aber bei unbekannten Strukturen nur einen
   Diagnosebericht liefern statt Ergebnisse.
6. Der Zugriff auf `~/Library/Application Support/MobileSync/Backup/` erfordert
   für das Terminal die Berechtigung „Festplattenvollzugriff".


---

## 18. Phase 7: lokales UI (vorgemerkt)

Nach der Extraktion soll der Export durchsehbar sein, ohne [Anzahl entfernt] Bilder im
Finder zu scrollen. Anforderungen, soweit sie jetzt schon feststehen:

**Datengrundlage.** Ausschließlich `export-manifest.json` und die exportierten
Dateien. Das UI liest die Threema-Datenbank nicht erneut — deshalb trägt das
Manifest bereits Chat, Zeitstempel, Originaldateiname, Medientyp und die
Zuordnung Vorschau ↔ Original.

**Vorschaubilder.** Die aus dem Backup übernommenen Thumbnails (Ø 58 KB) werden
direkt verwendet. Es wird keine Bildbibliothek eingebunden, um Vorschauen zu
erzeugen — das wäre eine zusätzliche Dependency ohne Not.

**Sicherheitsmodell.** Es gilt unverändert: keine Netzwerkverbindungen, kein
CDN, keine externen Fonts, keine Telemetrie. Das UI läuft entweder als eine
einzelne, in sich geschlossene HTML-Datei im Export oder über einen lokalen
Server, der ausschließlich an `127.0.0.1` bindet und nur aus dem
Ausgabeverzeichnis liest. Beides ist offline vollständig funktionsfähig.

**Offene Entscheidungen.** Einzeldatei-HTML gegen lokalen Server; ob
Nachrichtentexte überhaupt angezeigt werden (bisher werden sie nicht
exportiert); Umfang der Suche und Filter. Wird entschieden, wenn Phase 4 und 5
stehen und der Export in seiner endgültigen Form vorliegt.

---

## 19. Nachtrag: Erkenntnisse aus dem echten Backup (2026-08-20)

Die Analyse eines echten Backups (iPhone, iOS, unverschlüsselt, [Menge entfernt],
Threema `ch.threema.iapp` Version [Version entfernt]) hat die folgenden Punkte belegt. Sie
sind die Grundlage für Phase 4 und 5 und ersetzen dort jede Annahme.

### 19.1 Ablage der Threema-Daten

```
AppDomainGroup-group.ch.threema/
├── ThreemaData.sqlite                      [Menge entfernt], 26 Tabellen, Core Data
└── .ThreemaData_SUPPORT/_EXTERNAL_DATA/    [Anzahl entfernt] Dateien, [Menge entfernt], UUID-Namen
```

`ThreemaData.sqlite` ist ein lesbarer Core-Data-Store, **nicht** app-eigen
verschlüsselt. Zusätzlich vorhanden: `AppDomain-ch.threema.iapp` (28 Dateien,
App-Interna) und zwei Plugin-Domains für Notification- und Share-Extension.

### 19.2 Referenzformat externer Blobs

Eine `ZDATA`-Spalte enthält entweder die Daten selbst oder eine Referenz auf
`_EXTERNAL_DATA`. Die Referenz ist genau 38 Byte:

```
0x02 | 36 Byte UUID als ASCII (Großbuchstaben) | 0x00
```

Verifiziert: [Anzahl entfernt] Referenzen lösen auf eine vorhandene Datei auf.
Unterscheidungskriterium ist damit `len(blob) == 38 and blob[0] == 0x02`.

### 19.3 Richtung der Beziehungen

Core Data deklariert **keine** SQL-Fremdschlüssel; die Richtung liegt je
Beziehung nur auf einer Seite und muss empirisch geprüft werden. Gemessen:

| Join | Treffer | Trägt |
|---|---|---|
| `ZMESSAGE.ZDATA` → `ZFILEDATA.Z_PK` | [Anzahl entfernt] / [Anzahl entfernt] | ja |
| `ZMESSAGE.ZTHUMBNAIL` → `ZIMAGEDATA.Z_PK` | [Anzahl entfernt] / [Anzahl entfernt] | ja |
| `ZMESSAGE.ZIMAGE` → `ZIMAGEDATA.Z_PK` | 9 / 9 | ja |
| `ZMESSAGE.ZTHUMBNAIL1` → `ZIMAGEDATA.Z_PK` | 22 / 22 | ja |
| `ZMESSAGE.ZTHUMBNAIL2` → `ZIMAGEDATA.Z_PK` | 1 / 1 | ja |
| `ZMESSAGE.ZVIDEO` → `ZVIDEODATA.Z_PK` | 1 / 1 | ja |
| `ZFILEDATA.ZMESSAGE` → `ZMESSAGE.Z_PK` | [Anzahl entfernt] / [Anzahl entfernt] | ja |
| `ZIMAGEDATA.ZMESSAGE` → `ZMESSAGE.Z_PK` | **0 / [Anzahl entfernt]** | **nein** |
| `ZMESSAGE.ZCONVERSATION` → `ZCONVERSATION.Z_PK` | [Anzahl entfernt] / [Anzahl entfernt] | ja |

`ZIMAGEDATA.ZMESSAGE` ist vollständig verwaist. Wer dort joint, erhält null
Treffer und hält die Chat-Zuordnung für unmöglich. Das Profil muss die Richtung
jeder Beziehung zur Laufzeit prüfen und die tragende verwenden.

### 19.4 Zwei Quellen für Mediendaten

Ein Extractor, der nur Dateien aus `_EXTERNAL_DATA` kopiert, verliert
Originalmedien **stillschweigend**:

| Quelle | Anzahl | Größe |
|---|---|---|
| Externe Blob-Dateien | [Anzahl entfernt] | [Menge entfernt] |
| Inline-Originale (nur in der DB) | 714 | [Menge entfernt] |
| Inline-Thumbnails (nur in der DB) | [Anzahl entfernt] | [Menge entfernt] |

Beide Quellen laufen durch dieselbe SHA-256-Integritätsprüfung; bei
Inline-Blobs ist der Datenbankwert die Quelle statt einer Datei.

### 19.5 Verfügbare Metadaten

`ZMESSAGE` liefert `ZFILENAME` (Originaldateiname), `ZMIMETYPE`, `ZDATE`.
`ZCONVERSATION` liefert `ZGROUPNAME` für Gruppen und `ZCONTACT` für
Einzelchats. Gemessene Abdeckung: [Anzahl entfernt] externen Dateien sind einem
Chat belegbar zuordenbar, 277 nicht (→ `unassigned/`). 36 Konversationen,
davon 12 Gruppen; 84 Kontakte.

### 19.6 Nicht zu exportierende Dateien

Im Threema-Container liegen App-Interna, die keine Nutzdaten sind: 12 PLIST,
2 LOG sowie `observations.db` und `tips-store.db` (Apple-Frameworks). Sie gehen
nach `metadata/` bzw. `databases/`, nicht in die Medienverzeichnisse.


---

## 20. Nachtrag: Core-Data-Markierungsbyte (2026-08-20)

Beim Probelauf am echten Backup fiel auf, dass 434 Dateien in der Kategorie
`other` landeten und 399 als Video galten, obwohl das Backup nur 176
Videodateien enthält. Ursache war ein Byte:

Core Data stellt **jedem** Blob in einer Spalte mit „Allows External Storage"
ein Markierungsbyte voran:

| Byte 0 | Bedeutung |
|---|---|
| `0x01` | Die Daten folgen unmittelbar (inline). |
| `0x02` | Es folgt eine 36-Byte-UUID und `0x00` — Referenz auf `_EXTERNAL_DATA`. |

Am echten Backup ausgezählt: alle [Anzahl entfernt] Inline-Blobs beginnen mit `0x01`, alle
[Anzahl entfernt] Referenzen mit `0x02`. Ohne Abschneiden des `0x01`:

* Jede aus der Datenbank exportierte Datei wäre um ein Byte verschoben, also
  unbrauchbar — [Anzahl entfernt] davon JPEGs.
* Die Signaturerkennung greift nicht, weil die Magic Bytes an Offset 1 stehen.
  Der Typ wird dann über den Dateinamen geraten, was 399 statt 179 Videos ergab.

Nach dem Abschneiden sind [Anzahl entfernt] der [Anzahl entfernt] Blobs per Signatur eindeutig ([Anzahl entfernt]
JPEG, 18 PNG, 8 ISO-BMFF, 1 PDF, 1 GIF; 2 bleiben unbekannt).

Umsetzung: `MediaSource.byte_offset` trägt die Zahl der zu überspringenden
Bytes, gesetzt vom App-Profil und angewendet in `extract/sources.py`. Ein
unerwarteter erster Wert wird **gemeldet, nicht abgeschnitten** — falsch
abzuschneiden wäre schlimmer, weil es unbemerkt bliebe.

Lehre für die Fixtures: der Fehler existierte, weil der Fixture-Generator das
Markierungsbyte nicht setzte. Ein Fixture, das einfacher ist als die
Wirklichkeit, beweist nichts. Der Generator setzt es jetzt.

---

## 21. Ergebnis am echten Backup (2026-08-20)

| | |
|---|---|
| Extrahiert | [Anzahl entfernt] Dateien, [Menge entfernt] |
| Fehlgeschlagen | 0 |
| Integritätsfehler | 0 |
| Laufzeit | [Dauer entfernt] |
| Chat-Zuordnung | [Anzahl entfernt] ([Anteil entfernt]), 699 nach `unassigned/` |
| Chats | 25 |
| Per Hardlink gespart | [Menge entfernt] |
| `verify` | [Anzahl entfernt] in Ordnung |

Stichprobe von 400 exportierten Bildern: alle mit gültiger Signatur, JPEGs mit
korrekter Endmarke `FFD9`, keine Datei mit verbliebenem `0x01`-Vorspann.

Das Backup blieb unverändert; es entstanden keine SQLite-Nebenprodukte.
