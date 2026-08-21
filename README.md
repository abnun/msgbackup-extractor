# msgbackup-extractor

**English** · [Deutsch](README.de.md) · [Website](https://abnun.github.io/msgbackup-extractor/)

Recover your own photos, videos, voice messages and documents from a local
Apple iPhone backup — Threema and WhatsApp — without handing anything to a
cloud service.

It runs entirely on your machine, opens the backup **read-only**, and verifies
every file it writes with SHA-256. Where it cannot be sure, it says so instead
of guessing.

Developed and verified on **macOS**. **Windows** is implemented but has not been
run against a real backup — see [Requirements](#requirements).

```bash
msgx analyze  --backup "…/MobileSync/Backup/<UDID>"          # what is in there?
msgx extract  --backup "…" --output ~/export/threema          # write it out
msgx ui       --output ~/export                                # browse it
```

---

## What you get

A folder you can use like any other: photos, videos, voice messages and
documents, sorted twice over — once by media type, once by chat — plus a page
you double-click to look through them.

```
export/threema/
├── media/
│   ├── images/  videos/  audio/  documents/
│   └── thumbnails/
├── chats/
│   ├── Anna/{images,videos,audio,documents}/
│   ├── Familie/…
│   └── unassigned/       kept and visible, not discarded
├── databases/
└── index.html            double-click it, no server needed
```

The chat folders cost **no** extra disk space — they are hardlinks to the same
files. `index.html` is a timeline, newest first, with filters for media type,
year and chat, plus a search box. It is self-contained: no server, no network,
nothing to install.

What it cannot do is hand you a ZIP. A page opened from a file may *show* your
files but not *read* them, so you select in the browser and `msgx collect`
copies the selection out.

Message texts are **not** exported.

---

## How it works

Six steps, each its own command. You can stop after any of them.

```
  iPhone
    │  local backup (Finder on macOS, Apple Devices app on Windows)
    ▼
  MobileSync/Backup/<UDID>
    │
    │  msgx backups     which backups are on this machine?
    │  msgx analyze     what is inside? (read-only, writes nothing)
    │  msgx database    what does the app's database schema look like?
    ▼
  msgx extract  ───────────────────────────────────────┐
    │  reads the backup strictly read-only             │
    ▼                                                  │
  export/threema/                                      │
    ├── media/ chats/ databases/ metadata/             │
    └── export-manifest.json  ◄──── the record of it ──┘
    │
    │  msgx ui        builds index.html from the manifest
    │  msgx verify    checks the export against the manifest
    ▼
  index.html in your browser
    │  select, "hand over selection", copy the list
    ▼
  msgx collect     gathers the selection into a folder
```

`export-manifest.json` is the hub: `extract` writes it, and `verify`, `ui` and
`collect` read nothing else. That is why changing the UI costs no new export,
and why `verify` still works years later on a copy without the backup.

### What happens in each step

**Open the backup.** A backup is four metadata files plus 256 directories `00`
… `ff` holding payload files named by their `fileID` (SHA-1 of
`"<domain>-<relativePath>"`). `Info.plist` and `Manifest.plist` are readable
even in an encrypted backup, which is why app detection works without a
password.

**Decrypt if needed.** The keybag comes from `Manifest.plist`; PBKDF2 derives
the passcode key, AES Key Wrap unwraps the class keys, and `Manifest.db` is
decrypted into a temporary directory **outside** the backup. A wrong password
fails the key wrap's integrity check — you get a clear error, never garbage.

**Identify the messenger.** Not by guessing paths, but by the bundle
identifier actually present in the backup metadata. A namespace is searched
(`ch.threema.`) so variants are found; several matches lead to a question, not
to a pick.

**Locate the media.** This is where the work is. Threema stores blobs, partly
*inside* its database; WhatsApp stores real files and records their paths.
Which side of a Core Data relationship carries the foreign key is **measured at
runtime**, because it differs per entity.

**Extract.** A planner first computes what would happen — that same plan backs
`--dry-run` *and* the real run, so a rehearsal cannot behave differently. Then
each file is written on its own: hash the source while writing, read the
destination hash back, compare. A broken file costs one report entry, not the
run.

**Browse and pull out.** `msgx ui` builds a self-contained `index.html` from
the manifest. Selecting happens in the browser; copying is done by the CLI,
because JavaScript on a `file://` page may *display* local files but cannot
*read* their bytes.

### What it never does

The backup is **only read**. SQLite connections use `mode=ro&immutable=1` so
not even a `-wal` file appears next to the original. Writes go exclusively to
the directory given as `--output`, and a guard checks every destination path
against it.

There is **no network code**. Everything platform-specific lives in one module
(`core/platforms.py`), and two guard tests keep it there.

Full architecture and every measured finding:
[`docs/specs/2026-08-20-messenger-backup-extractor-design.md`](docs/specs/2026-08-20-messenger-backup-extractor-design.md)
(in German).

---

## Supported messengers

| | Threema | WhatsApp | Signal |
|---|---|---|---|
| Status | complete, verified on real data | complete, verified on real data | detected only |
| Database | `ThreemaData.sqlite` | `ChatStorage.sqlite` | not in the backup |
| Media | blobs, partly *in* the database | files under `Message/Media/` | — |
| Reference | `0x02` + UUID → `_EXTERNAL_DATA/` | path in the DB, missing the `Message/` prefix | — |
| Chat names | `ZCONVERSATION` | `ZWACHATSESSION.ZPARTNERNAME` | — |
| Relationship direction | one side is **fully orphaned** | both sides carry | — |

Both are Core Data stores, so they share the epoch (2001) and the
direction-measuring logic. Measuring rather than assuming is not academic: in
Threema, `ZIMAGEDATA.ZMESSAGE` is 100 % orphaned. Join there and you conclude
chat assignment is impossible.

### Signal cannot be extracted

Signal is detected, but there is nothing to get: the app excludes its data
directory from iOS backups. In the backup measured here, five Signal domains
held **a handful of files totalling a few dozen kilobytes** — preference plists,
WebKit caches, a lock file. No message database, no media.

The profile exists precisely to say so, rather than letting an empty result
look like a bug in this tool. Signal data moves via Signal's own path (device
transfer or a Signal backup).

Adding a messenger means writing one profile. `AppProfile` survived WhatsApp
without a change, even though its storage model is fundamentally different.

---

## Requirements

| | macOS | Windows |
|---|---|---|
| OS | macOS (developed and verified here) | Windows 10 or 11 (see below) |
| Python | 3.12 or newer | 3.12 or newer |
| Backup | local Finder backup | local backup from the Apple Devices app or iTunes |
| Extra | Full Disk Access for the terminal | nothing — backups live in your user profile |
| Disk space | roughly the size of the messenger's data | same |

The chat structure costs **no** extra space: it is hardlinked to the same data.
Without them the chat structure would double the space needed. The backup itself
is never copied.

### Windows is untested

The core is platform-independent: SQLite, plistlib, hashlib, `cryptography` and
the format parsing run anywhere Python runs. Exactly four things depend on the
operating system, and they all live in `core/platforms.py`: where backups are
stored, which folders sync to a cloud, how the clipboard reaches a pipe, and
what to do about missing permissions.

The Windows paths come from Apple's documentation and the usual behaviour of
the Apple Devices app — **not from a test run on a Windows machine**. Tests fake
the operating system and check that the right paths and hints come out; they
cannot confirm that Apple actually stores backups there.

If no backup is found, `--backup` with a full path works and the rest of the
pipeline does not care. A report from anyone who tries it is welcome.

---

## Installation

### macOS

```bash
python3 --version                      # 3.12 or newer

git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

python3 -m venv ~/.venvs/msgbackup-extractor    # NOT inside iCloud Drive, see below
~/.venvs/msgbackup-extractor/bin/pip install -e ".[dev]"

~/.venvs/msgbackup-extractor/bin/msgx --version
~/.venvs/msgbackup-extractor/bin/python -m pytest
```

Convenient with an alias in `~/.zshrc`:

```bash
alias msgx="$HOME/.venvs/msgbackup-extractor/bin/msgx"
```

**Grant Full Disk Access.** Without it the terminal cannot read
`~/Library/Application Support/MobileSync/Backup/`: System Settings → Privacy &
Security → Full Disk Access → add Terminal → **quit and restart Terminal**. The
restart is required; the change does not apply to a running process.

**Do not put the venv inside iCloud Drive.** The iCloud file provider sets the
macOS `UF_HIDDEN` flag on every `.pth` file, and Python 3.12 skips hidden
`.pth` files (`Lib/site.py`, `addpackage()`). The editable install is then
**silently ignored** and `import msgbackup_extractor` fails. `chflags nohidden`
does not stick — the flag returns within seconds. Verify with:

```bash
PYTHONVERBOSE=1 python -c "pass" 2>&1 | grep "Skipping hidden"
```

### Windows

```powershell
py --version                           # 3.12 or newer

git clone https://github.com/abnun/msgbackup-extractor.git
cd msgbackup-extractor

py -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"

.\.venv\Scripts\msgx --version
.\.venv\Scripts\python -m pytest
```

`msgx backups` searches both usual locations, because iTunes and the Apple
Devices app use different directories:

```text
%APPDATA%\Apple Computer\MobileSync\Backup     (iTunes)
%USERPROFILE%\Apple\MobileSync\Backup          (Apple Devices app)
```

If nothing is found, the message names both paths and their state. Then use the
full path:

```powershell
.\.venv\Scripts\msgx analyze --backup "C:\Users\YOU\Apple\MobileSync\Backup\<UDID>"
```

---

## Where to put the data

Use a **local, unsynced** directory, with one subdirectory per messenger. The
Apple backup is the shared source; exports are kept apart:

```
~/messenger-extract/
├── backup/            the Apple backup (read-only; a symlink is fine)
│   └── <UDID>/
└── export/
    ├── threema/
    ├── whatsapp/
    └── index.html     combined view across all exports
```

The tool refuses an `--output` inside a cloud-synced container, because the
operating system would then upload your extracted messages. It knows the usual
locations per platform (iCloud Drive, OneDrive, Dropbox, Google Drive, pCloud,
Nextcloud, ownCloud, Seafile, MEGA, Sync.com). Detection is path-based and
therefore offline; an arbitrarily configured sync folder is beyond it.
`--allow-cloud-output` overrides the refusal deliberately.

---

## Usage

### Double-click instead of typing

There is a small macOS bundle for people who would rather not open a terminal
themselves:

```bash
scripts/build-app.py            # writes msgbackup-extractor.app to ~/Applications
```

It is built on your machine from what macOS already has — nothing is
downloaded, no framework is installed. The icon is generated as PNG in pure
Python and `iconutil` turns it into an `.icns`.

**It opens a Terminal, and that is deliberate.** An encrypted backup needs a
password, and the password is only ever typed in — never passed as an argument,
never fetched from a keychain, never collected by a window that would have to
put it somewhere. A silent graphical wrapper would be a step backwards here, so
the bundle is a *launcher*, not a second application.

What it launches is `msgx guide`, which you can also run yourself:

```bash
msgx guide
```

It asks for the backup, the messenger and the output directory, runs `analyze`,
then a rehearsal, and only extracts after an explicit yes. **Every step prints
the command it is about to run** — so it teaches the tool rather than hiding it,
and the last thing it prints is the one command you need next time.

On **Windows** the same command writes a double-clickable
`msgbackup-extractor.cmd` to the desktop instead. It opens a console window for
the same reason. Like the rest of the Windows support, it has never been run
there. On any other system the script refuses rather than producing something
unusable — `msgx guide` works there directly.

The path to `msgx` is baked in at build time, because a bundle cannot know which
environment you meant. Move or delete the environment and the launcher says so
on the next start, instead of silently doing nothing.

The build **tries** each candidate rather than trusting that it exists: it runs
`msgx --version` and only accepts one that succeeds. That is not pedantry — a
virtual environment inside iCloud Drive is present and executable and still
fails, so a bundle pointing at one would only break on the first double-click.

### Find backups

```bash
msgx backups
```

Lists the backups on this machine with device name, iOS version and whether
they are encrypted.

### Analyze

```bash
msgx analyze --backup "~/messenger-extract/backup/<UDID>"
```

Reports device, iOS version, encryption state, detected messengers with bundle
identifier and version, their domains, the media formats actually found, and
the SQLite databases identified. Writes nothing.

| Option | Effect |
|---|---|
| `--app threema` | check only one messenger |
| `--bundle-id ID` | resolve an ambiguous detection |
| `--metadata-only` | do not ask for the password of an encrypted backup |
| `--json PATH` | also write the report as JSON |
| `--include-schema` | include the full manifest schema in the JSON |
| `--no-media-inspection` | do not read payload files (faster, no format statistics) |
| `--verbose` | technical detail; still no message content |
| `--show-paths` | show file paths in clear instead of masked |

### Extract

Rehearse first — this writes nothing:

```bash
msgx extract --backup "~/messenger-extract/backup/<UDID>" \
             --output  "~/messenger-extract/export/threema" --dry-run
```

Then for real, without `--dry-run`. Result:

```
export/threema/
├── media/
│   ├── images/ videos/ audio/ documents/ other/
│   └── thumbnails/          the app's own previews, linked to their originals
├── chats/
│   ├── <chat name>/{images,videos,audio,documents,thumbnails}/
│   └── unassigned/          everything without provable assignment
├── databases/               the app databases
├── metadata/                app internals (plists, logs)
├── reports/extraction-report.json
├── export-manifest.json
└── index.html               the local view, rebuilt automatically
```

`media/` and `chats/` point at the same data via **hardlinks**, so the space is
used once.

| Option | Effect |
|---|---|
| `--dry-run` | show what would happen, write nothing |
| `--no-organize-by-chat` | only `media/`, no chat structure |
| `--no-hardlinks` | real copies instead of hardlinks (double the space) |
| `--no-thumbnails` | skip the app's preview images |
| `--deduplicate` | write identical content only once |
| `--types image,video` | restrict to these categories |
| `--no-ui` | do not rebuild the local view |
| `--allow-cloud-output` | force output into a synced folder |

**The view is rebuilt automatically.** After a successful export, `extract`
rewrites `index.html` in the output directory. The **combined** view lives in
the parent directory — outside `--output`, where this tool has promised not to
write. So:

| Parent directory contains | What `extract` does |
|---|---|
| an `index.html` created by `msgx ui` | updates it too — you already chose that directory as the overview |
| no `index.html`, but other exports | writes nothing; the report prints the `msgx ui` command |
| a **foreign** `index.html` | leaves it alone — replacing it would be data loss |

Recognition is by `<meta name="generator" content="msgbackup-extractor">` in
the file head. Pages from an older version lack it and are left untouched; one
`msgx ui` run brings them up to date.

If building the view fails, that is a **notice, not an error**: the files are
already written and their hashes verified.

### Browse

```bash
msgx ui --output "~/messenger-extract/export/threema"    # one messenger
msgx ui --output "~/messenger-extract/export"            # all of them, one page
```

Point `--output` at an export directory for a single-messenger page; point it
at a directory containing several exports and you get **one page** with a
switcher (`All | Threema | WhatsApp`) and the messenger as another filter.

Double-click the file. No server needed.

The page is **self-contained**: no CDN, no external fonts, no network request,
no telemetry. Icons are inline SVG rather than emoji so they look the same
everywhere. The index is embedded rather than fetched, because `fetch()` from
`file://` fails the browser's same-origin rule. A test checks the page for
network references.

- **Timeline**, newest first, with month separators
- **Filters** for media type, year, chat and oddities (no chat, no date,
  preview only, extension ≠ content), plus filename search. Counts are
  **faceted**: they show what would remain if you picked that option, given
  every other active filter. Options leading nowhere are dimmed.
- **Single view** with the original, a video player, metadata and keyboard
  navigation (`←` `→` browse, `Space` select, `Esc` close)
- **Selection** via a checkmark per tile, Shift for a range, and "select all in
  filter" for the active filter. What gets handed over is what you can see: the
  selection *inside the current filter*. Anything selected earlier and now
  hidden stays selected — the tray says how much — but it is not part of the
  commands until you widen the filter again. The count on screen therefore
  always matches the checkmarks.

Three deliberate decisions:

**Previews are not separate entries.** A preview is its original's tile —
otherwise every image would appear twice. A preview *without* an original stays
its own entry, marked "preview only": it is often all that remains of a deleted
file.

**Tiles pick the right resolution.** The previews the messengers store are
tiny — around 100 × 73 px for WhatsApp. In a 200 CSS-px tile on a Retina
display that is a fourfold upscale. So the manifest carries the real pixel
dimensions, read from the file header, and the page offers both resolutions per
tile via `srcset`. The browser picks the smallest sufficient source.

**Only message dates appear on the timeline.** For files that only carry a
filesystem timestamp from the backup — settings, logs, the databases — that
date would be misleading: it is the same day for all of them and would push the
newest real media off the top. They appear under "no date"; the single view
shows their file date separately.

### Pull out a selection

The selection dialog leads to:

For a small selection it is one command with the paths in it:

```bash
msgx collect \
    --output "~/messenger-extract/export/threema" \
    --target ~/selection \
    'media/images/a.jpg' 'media/videos/b.mp4'
```

For a large one the dialog splits it across several commands — `collect` may run
repeatedly into the same target, and it remembers what is already there. Only
when even that becomes unreasonable does it fall back to a list on standard
input:

```bash
pbpaste | msgx collect \
    --output "~/messenger-extract/export/threema" \
    --target ~/selection \
    --selection -
```

Whichever it is, the dialog shows **every** command you have to run, numbered,
each collapsed with its own copy button. The **target folder is a field in the
dialog** — change it and the commands change with it, correctly quoted for the
shell. There is no folder picker, and there cannot be one: a browser hands out
the folder's *name*, never its path.

On Windows the clipboard command is `powershell -Command Get-Clipboard`; the
dialog prints the right one for your system. `--selection FILE` also accepts a
text file with one relative path per line.

| Option | Effect |
|---|---|
| `--no-hardlinks` | real copies — needed if you want to pass the selection on |
| `--keep-structure` | preserve the export's directory layout |
| `--verify` | check each file's SHA-256 against the manifest |
| `--dry-run` | show only what would be gathered |

#### Why the browser cannot do this

Not even for a single file, and not because of the same-origin rule alone.
Measured:

```
fetch:              BLOCKED (TypeError: Failed to fetch)
XMLHttpRequest:     BLOCKED
<img> display:      OK
canvas readback:    BLOCKED (SecurityError — tainted canvas)
<a download> click: NOTHING HAPPENS — no download, no error
```

The last line is the one that decides it, and it needs a control to be worth
anything. The same page, the same anchor, the same `download` attribute, the
same genuine user gesture, an explicitly allowed download directory:

| Served over | Download events | Result |
|---|---|---|
| `http://` | `downloadWillBegin`, then progress | the file arrives |
| `file://` | **none at all** | nothing, and no error |

So it is the URL scheme, not the gesture and not the configuration. Chrome
silently declines to download anything from a page opened as a file.

What still works, because it is the browser's own function rather than the
page's: **right-click on the image → "Save image as…"**. For one file that is
the short way, and the selection dialog says so.

What would fix it properly is serving the export over `http://localhost` — and
that means opening a socket. This project promises that no module imports one,
and a test enforces it. The convenience is not worth breaking the one guarantee
the whole thing rests on. So the UI selects and the CLI gathers.

### Verify

```bash
msgx verify --manifest "~/messenger-extract/export/threema"
```

Checks existence, size and SHA-256 of every exported file, plus the hardlinks.
The backup is **not** required, so this also works on a copy of the export,
years later, on another machine.

### Inspect database schemas

```bash
msgx database --backup "~/messenger-extract/backup/<UDID>"
```

Prints tables, columns, types, primary and foreign keys of the app databases —
**no contents**.

---

## Encrypted backups

The primary case. The tool

1. reads the keybag from `Manifest.plist`,
2. derives the passcode key with PBKDF2 (double, SHA-1 then SHA-256, from
   keybag version 3 on),
3. unwraps the class keys with AES Key Wrap (RFC 3394),
4. decrypts `Manifest.db` with AES-256-CBC into a temporary directory
   **outside** the backup,
5. decrypts payload files on demand — for type detection only their first
   bytes.

### Which password?

The one you set in the Finder when you enabled "Encrypt local backup". **Not**
your iPhone passcode and **not** your Apple ID password. A wrong password fails
the AES Key Wrap integrity check and is reported as such; no garbage is
produced.

### How it is asked for

Interactively via `getpass.getpass()` only. There is **no** command-line
option, **no** environment variable and **no** config file — a password in
`argv` ends up in shell history and the process list. A test introspects every
subcommand to prove no such option exists.

An honest limit: CPython cannot guarantee secure erasure of immutable `str` and
`bytes`. Derived keys live in an overwritable buffer that is zeroed after use;
the password itself may linger in the heap until the process ends. That narrows
the window, it does not close it.

`--metadata-only` produces a **partial report** from the unencrypted plists:
device, iOS version, encryption state and the detected messengers. File and
media statistics are missing and are reported as missing, not as zero.

---

## Why you can check this yourself

Most people can skip this section. It is here for the reader who wants to know
why the promises above are worth anything, given that a tool touching a message
archive can promise whatever it likes.

Every guarantee below has a test that would fail if it stopped being true. The
tests ship with the source, so this is checkable without taking anyone's word:
`python -m pytest`.

| Guarantee | How it is verified |
|---|---|
| The backup is never modified | A fingerprint of every file — content, size, mtime — is taken before and after a full run and compared. Counter-tests confirm the fingerprint actually detects changes. |
| No network access | No source module imports `socket`, `urllib`, `http` or similar. Checked statically, and again dynamically with `socket` disabled. |
| Exported files are intact | The source SHA-256 is computed from the same bytes that get written; the destination hash is read back afterwards. Only the comparison counts as proof. |
| No password in logs or arguments | There is no password option. A test introspects every subcommand to keep it that way. |
| Nothing is guessed | Structure is measured at runtime. When it cannot be established, you get a diagnostic report instead of a result. |

Verified end to end against a real iPhone backup holding both messengers: every
exported file's SHA-256 matched, no write failures, no integrity errors. Over
90 % of the media could be assigned to a chat; the rest went to `unassigned/`
rather than being guessed.

The measurements behind that live in the design document. Concrete figures — how
many files, how large, how long — are deliberately absent here: they would
describe the author's own device and message volume, not the tool.

---

## Dependencies

| Package | Version | License | Purpose |
|---|---|---|---|
| [`cryptography`](https://github.com/pyca/cryptography) | >=42,<46 | Apache-2.0 OR BSD-3-Clause | PBKDF2, AES-256-CBC, AES Key Wrap (RFC 3394) |
| `cffi` (transitive) | — | MIT-0 | bindings for `cryptography` |
| `pycparser` (transitive) | — | BSD-3-Clause | dependency of `cffi` |
| `pytest`, `pytest-cov`, `ruff` (dev only) | — | MIT | tests and linting |

Everything else is the Python standard library. **No cryptographic primitive is
implemented here** — own code is limited to format parsing (TLV keybag,
NSKeyedArchiver plists, JPEG/PNG/GIF/WEBP headers).

Nothing is downloaded at runtime.

---

## Tests

```bash
~/.venvs/msgbackup-extractor/bin/python -m pytest
~/.venvs/msgbackup-extractor/bin/ruff check src tests
```

704 tests. They **never** need real private data: every backup is generated
synthetically with a real TLV keybag, real PBKDF2, real AES Key Wrap, real
AES-256-CBC and real NSKeyedArchiver MBFile blobs, so they actually exercise
the production code instead of matching a simplified test format.

Four worth naming:

- `test_readonly_guarantee.py` fingerprints the whole backup, runs everything,
  and compares again. Counter-tests prove the fingerprint detects changes.
- `test_no_network.py` checks statically that no source file imports a network
  module, and dynamically that the whole package imports with `socket`
  disabled.
- `test_analysis.py::test_encrypted_and_plain_reports_agree` analyses the same
  backup encrypted and unencrypted and compares the reports.
- `test_platforms.py` fakes the operating system so the Windows paths are
  checked rather than claimed.

Before pushing, a second gate runs:

```bash
scripts/check-sensitive.sh
```

It scans the working tree **and the whole commit history** for personal data,
machine-specific paths, device identifiers and leftovers from a history
rewrite, and exits non-zero on a hit. Known-harmless matches — synthetic
fixture data — are listed with a reason in
`scripts/sensitive-allowlist.txt`, rather than by weakening a pattern.

---

## Troubleshooting

**`Kein Leserecht auf …` / `PermissionError` (macOS)** — the terminal needs Full
Disk Access, and you must **restart** it afterwards.

**`Keine Backups gefunden` (Windows)** — the message names the searched paths
and their state. Common causes: the backup is in iCloud rather than local, or it
was moved to another drive. The full path via `--backup` still works.

**`Das Passwort ist falsch`** — it is the Finder backup password, not the device
passcode and not the Apple ID password.

**`… sieht nicht wie ein Apple-Backup aus`** — `--backup` must point at the
directory named after the device ID, not at `MobileSync/Backup` itself. `msgx
backups` lists the candidates.

**`Diagnosebericht: keine Dateitabelle gefunden`** — the backup has a structure
this tool does not know. It stops deliberately instead of guessing; the report
lists the tables and columns actually present and is the useful basis for a bug
report.

**`import msgbackup_extractor` fails** — is the venv inside iCloud Drive? See
[Installation](#macos).

**`pbpaste: command not found` (Windows)** — that is a macOS command; use
`powershell -Command Get-Clipboard`.

---

## Known limitations

1. Only data actually **present** in the backup can be extracted. Apps may
   exclude files — Signal excludes everything.
2. Protection classes tied to the device key alone cannot be opened from a
   backup. Affected files are counted as undecryptable, not silently skipped.
3. A manifest entry with an unreadable MBFile blob has no file key. In an
   encrypted backup its content is therefore ciphertext and its type cannot be
   determined; it is reported as undecryptable rather than typed from ciphertext.
4. A file truncated in the backup whose length happens to be a multiple of 16
   bytes decrypts without error — just to less data. The decrypted byte count is
   therefore always compared against the size recorded in the manifest.
5. Core Data prefixes every blob with a marker byte (`0x01` inline, `0x02`
   reference). It is stripped; an unexpected value is reported rather than
   removed blindly.
6. For many WhatsApp entries the backup holds **only** the preview image
   (typically around 100 px wide). Those tiles cannot be sharp; the single view
   says so.
7. Message texts are not exported. The UI therefore cannot show them.
8. Secure erasure of the password from the Python heap is not guaranteeable.
9. The cloud guard recognises the usual locations by path. An arbitrarily
   configured sync folder is beyond it.
10. **Windows is implemented but untested.** See
    [Requirements](#windows-is-untested).
11. For other systems (Linux, BSD) no default location is known and none is
    invented. A copied backup still works via `--backup`.

Messengers may change their internal structure between versions. Structure is
detected at runtime, but for an unknown structure the tool can only produce a
diagnostic report.

---

## Intended use

> [!IMPORTANT]
> This tool is for **your own** backup on **your own** machine.

Backups contain messages exchanged with other people. Under the GDPR, purely
private use falls under the household exemption (Art. 2(2)(c)); **publishing or
passing on extracted data does not — from that point you are the one
responsible.**

> [!CAUTION]
> Running it against another person's backup without authorisation may be a
> **criminal offence** — in Germany, for example, under section 202a StGB
> (Ausspähen von Daten). Do not do that.

Threema, WhatsApp and Signal are trademarks of their respective owners, named
here only to describe which formats can be read. This project is not affiliated
with, endorsed by, or sponsored by any of them. See [`NOTICE`](NOTICE).

---

## License

[Apache License 2.0](LICENSE). Copyright 2026 Markus Mueller.

Apache-2.0 rather than MIT for two reasons that matter here: it carries an
explicit warranty disclaimer and limitation of liability, and it requires
modified files to be marked as changed. If someone forks this and removes the
read-only guards or the integrity checks, that obligation makes the change
visible.
