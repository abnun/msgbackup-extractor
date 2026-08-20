# Messenger Backup Extractor — Produktwahrheit

> Alle Zahlen in diesem Dokument sind an einem echten Backup gemessen
> (iPhone, iOS, [Menge entfernt], unverschlüsselt) und in
> `docs/specs/2026-08-20-messenger-backup-extractor-design.md` belegt.

## Was es ist

Ein Kommandozeilenwerkzeug für macOS, das Messenger-Daten aus einem lokalen
Apple-iPhone-Backup identifiziert und in eine normale Dateistruktur exportiert.
Unterstützt: Threema, WhatsApp, Signal (Letzteres nur erkennend, siehe unten).

## Der einzigartige Mechanismus

**Es behauptet nichts, es weist nach.** Jede Zusage hat eine Prüfung:

| Zusage | Nachweis |
|---|---|
| Das Backup wird nie verändert | Fingerabdruck (Inhalt, Größe, mtime) jeder Datei vor und nach einem vollen Lauf — samt Gegenproben, dass der Test Veränderungen wirklich erkennt |
| Keine Netzwerkverbindung | Kein Quellmodul importiert `socket`, `urllib`, `http`; statisch geprüft und dynamisch mit gesperrtem `socket` |
| Der Export ist unverfälscht | SHA-256 der Quelle entsteht beim Schreiben, der des Ziels wird nachgelesen; erst der Vergleich gilt |
| Kein Passwort im Log | Es gibt kein Passwort-Argument; per Introspektion aller Unterbefehle erzwungen |
| Nichts wird geraten | Struktur wird gemessen; bei Unklarheit entsteht ein Diagnosebericht statt eines Ergebnisses |

## Die Zielgruppe und ihre Lage

Eine technisch versierte Person, die an ihre **eigenen** Familienfotos und
Sprachnachrichten aus Jahren Chatverlauf will — ohne sie einem Cloud-Dienst
anzuvertrauen. Nicht ein Forensiker im Auftrag, sondern jemand, der sein eigenes
Archiv zurückholt. Sitzt am Mac, hat ein Finder-Backup, kann ein Terminal
öffnen, will aber Belege sehen statt Versprechen.

## Was am echten Backup herauskam

| | Threema | WhatsApp |
|---|---|---|
| Extrahiert | [Anzahl entfernt] Dateien, [Menge entfernt] | [Anzahl entfernt] Dateien, [Menge entfernt] |
| Fehler / Integritätsfehler | 0 / 0 | 0 / 0 |
| Einem Chat zugeordnet | [Anteil entfernt] | [Anteil entfernt] |
| Laufzeit | [Dauer entfernt] | ~6 Minuten |

Signal: erkannt (v1799.0), aber **nicht extrahierbar** — die App schließt ihr
Datenverzeichnis vom iOS-Backup aus. Im Backup lagen zwölf Dateien mit 41 KB.
Das Profil existiert, um diesen Grund zu nennen.

## Was nur dieses Produkt beweisen kann

Fehler, die nur ein echtes Backup zeigt, und die es gefunden hat:

* **Ein Byte.** Core Data stellt jedem Blob eine Markierung voran (`0x01`
  inline, `0x02` Referenz). Ohne Abschneiden wären [Anzahl entfernt] aus der Datenbank
  exportierte Dateien um ein Byte verschoben — [Anzahl entfernt] davon JPEGs, unbrauchbar —
  und die Signaturerkennung hätte den Typ per Dateiname geraten: 399 statt 179
  Videos.
* **Zwei Epochen.** MBFile zählt ab 1970, Core Data ab 2001. Verwechselt man
  sie, landen Datumsangaben 31 Jahre in der Zukunft. Von [Anzahl entfernt] Zeitstempeln
  ergaben [Anzahl entfernt] mit der Unix-Epoche ein plausibles Datum und 0 mit der anderen.
* **Eine verwaiste Richtung.** Core Data legt den Fremdschlüssel je Beziehung
  nur auf einer Seite ab. Bei Threema ist `ZIMAGEDATA.ZMESSAGE` zu 100 %
  verwaist — wer dort joint, hält die Chat-Zuordnung für unmöglich. Die Richtung
  wird deshalb zur Laufzeit gemessen.

## Abhängigkeiten

Genau **eine** zur Laufzeit: `cryptography` (PyCA), Apache-2.0 oder BSD-3.
Alles andere ist Standardbibliothek. Es wird keine eigene Kryptografie
implementiert; eigener Code ist Formatparsing.

## Was es nicht ist und nicht kann

* Kein Zugriff auf das iPhone selbst. Kein Jailbreak.
* Es kann nur extrahieren, was im Backup **enthalten** ist. Apps können Dateien
  ausschließen — Signal tut genau das.
* Keine Nachrichtentexte. Sie werden nicht exportiert.
* Sicheres Löschen des Passworts aus dem Python-Heap ist nicht garantierbar.

## Zweckbestimmung und Grenzen

Gedacht für das **eigene** Backup auf dem **eigenen** Rechner. Die Daten
enthalten Nachrichten Dritter; für rein privaten Gebrauch greift die
Haushaltsausnahme (Art. 2 Abs. 2 lit. c DSGVO), beim Weitergeben nicht mehr. Auf
das Backup einer anderen Person angewendet kann es § 202a StGB berühren.

Threema, WhatsApp und Signal sind Marken ihrer jeweiligen Inhaber. Es besteht
keine Verbindung zu ihnen.

## Markenverpflichtungen

Keine. Es gibt kein bestehendes Logo, keine Hausfarben, keine Schrift.

## Was diese erste Oberfläche beweisen muss

Dass Nachprüfbarkeit hier keine Marketingfloskel ist. Jemand, der die Seite
verlässt, soll gesehen haben, *woran* er das überprüfen kann — nicht gelesen
haben, dass es sicher sei.
