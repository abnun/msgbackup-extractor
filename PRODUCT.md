# Messenger Backup Extractor — Produktwahrheit

> Alle Aussagen in diesem Dokument sind an einem echten Backup gemessen und in
> `docs/specs/2026-08-20-messenger-backup-extractor-design.md` belegt. Konkrete
> Stückzahlen, Datenmengen und Gerätedaten stehen absichtlich nirgends: sie
> beschreiben das Gerät und das Nachrichtenvolumen des Autors, nicht das
> Produkt.

## Was es ist

Ein Kommandozeilenwerkzeug für macOS, das Messenger-Daten aus einem lokalen
Apple-iPhone-Backup identifiziert und in eine normale Dateistruktur exportiert.
Unterstützt: Threema, WhatsApp, Signal (Letzteres nur erkennend, siehe unten).

## Der Nutzen

**Jahre an Fotos, Videos und Sprachnachrichten kommen als normale Ordner
zurück** — sortiert nach Chat und Datum, durchsehbar im Browser, ohne dass ein
Cloud-Dienst sie anfasst. Wer sie extrahiert hat, kann sie danach behandeln wie
jede andere Datei: kopieren, sichern, verschenken, drucken.

Das ist der Grund, aus dem jemand das Werkzeug sucht. Alles Weitere ist die
Antwort auf die Frage, ob man ihm dabei trauen kann.

## Der einzigartige Mechanismus

**Es behauptet nichts, es weist nach.** Das ist die Eigenschaft, die es von
anderen Extraktoren unterscheidet — aber es ist die Antwort auf eine Rückfrage,
nicht der Grund für den Besuch. Auf einer Startseite gehört sie hinter den
Nutzen, nicht davor. Jede Zusage hat eine Prüfung:

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
| Extrahiert | vollständig | vollständig |
| Fehler / Integritätsfehler | 0 / 0 | 0 / 0 |
| Einem Chat zugeordnet | über 90 % | über 90 % |
| Laufzeit | unter zwei Minuten | wenige Minuten |

Signal: erkannt, aber **nicht extrahierbar** — die App schließt ihr
Datenverzeichnis vom iOS-Backup aus. Im Backup lag nur eine Handvoll Dateien mit
wenigen Dutzend Kilobyte. Das Profil existiert, um diesen Grund zu nennen.

## Was nur dieses Produkt beweisen kann

Fehler, die nur ein echtes Backup zeigt, und die es gefunden hat:

* **Ein Byte.** Core Data stellt jedem Blob eine Markierung voran (`0x01`
  inline, `0x02` Referenz). Ohne Abschneiden wäre **jede** aus der Datenbank
  exportierte Datei um ein Byte verschoben — die große Mehrheit davon JPEGs,
  unbrauchbar — und die Signaturerkennung hätte den Typ per Dateiname geraten:
  mehr als doppelt so viele angebliche Videos wie tatsächlich vorhanden.
* **Zwei Epochen.** MBFile zählt ab 1970, Core Data ab 2001. Verwechselt man
  sie, landen Datumsangaben 31 Jahre in der Zukunft. Bei jedem geprüften
  Zeitstempel ergab die Unix-Epoche ein plausibles Datum und die andere in
  keinem einzigen Fall.
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
Haushaltsausnahme (Art. 2 Abs. 2 lit. c DSGVO), beim Weitergeben nicht mehr —
dann ist der Anwender verantwortlich. Auf das Backup einer anderen Person
angewendet kann es § 202a StGB berühren.

Daraus folgt auch etwas für die Oberflächen dieses Projekts: **sie dürfen keine
Zahlen über den Bestand ihres Autors nennen.** Gerätemodell, Backup-Größe,
Dateizahlen und Chat-Zahlen sind zusammen ein Profil einer identifizierbaren
Person. Nachweise gehören dazu, Selbstauskünfte nicht.

Threema, WhatsApp und Signal sind Marken ihrer jeweiligen Inhaber. Es besteht
keine Verbindung zu ihnen.

## Markenverpflichtungen

Keine. Es gibt kein bestehendes Logo, keine Hausfarben, keine Schrift.

## Was diese erste Oberfläche leisten muss

Jemand landet dort, weil er nach „WhatsApp-Bilder aus iPhone-Backup holen"
gesucht hat. Er soll in wenigen Sekunden wissen:

1. **Kommt mein Zeug raus?** Welche Messenger, welche Dateiarten.
2. **Was bekomme ich?** Ordner nach Chat und Datum, plus eine Seite zum
   Durchsehen. Das ist das Ergebnis, und es sollte man sehen können.
3. **Was brauche ich?** Einen Mac, ein lokales Backup, ein Terminal. Ehrlich
   gesagt, nicht schöngeredet: es ist ein Kommandozeilenwerkzeug.
4. **Kann ich dem trauen?** Nur lesend, kein Netz, Hashes geprüft — kurz, und
   mit Weg zum Nachlesen für die, die es genauer wollen.
5. **Wie fange ich an?**

**Was die Seite nicht sein darf:** eine Nachweis-Tabelle als Aufmacher. Die
Nachprüfbarkeit ist das Beste an diesem Projekt, aber sie beantwortet Frage 4,
nicht Frage 1. Wer mit Punkt 4 anfängt, redet über sich selbst statt über den
Besucher.
