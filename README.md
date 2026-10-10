# JoyCheck – Joybuy-Preisvergleich

> ⚠️ Privates Hobbyprojekt. Mehrere Shops untersagen automatisierten Zugriff in ihren AGB – bitte vor der Nutzung den
> [rechtlichen Hinweis](#️-rechtlicher-hinweis--disclaimer) lesen.

Ist [Joybuy.de](https://www.joybuy.de) (JD.com Europa) wirklich günstiger? **JoyCheck** crawlt Joybuy-Artikel,
sucht dieselben Produkte bei **Amazon, REWE, ALDI SÜD, ALDI Nord, Lidl und idealo** (optional dm) und zeigt alles in einer Webshop-Oberfläche –
mit Filtern für „Joybuy günstiger“, Mindest-Ersparnis, Joybuy-Rabatt (UVP), Kategorie, Preis und Shop.

![Screenshot](docs/screenshot.png)

## Features

- **Webshop-UI**: Produktkacheln mit Bild, Preis, UVP-Rabatt, Grundpreis und den günstigsten Vergleichspreisen; Endlos-Scrollen
- **Filter**: Joybuy günstiger/teurer · Mindest-Ersparnis in % · Mindest-Joybuy-Rabatt · Kategorie · Crawl-Suchbegriff ·
  Preisbereich · nur auf Lager · Shop-Auswahl (Vergleich nur gegen ausgewählte Shops)
- **Sortierung**: größte Ersparnis (% / €), höchster Joybuy-Rabatt, Joybuy am teuersten, Preis, Bewertung, neu
- **Detailansicht**: Vergleichstabelle aller Shops mit Match-Qualität, EAN (REWE/dm), Links und Preisverlauf
- **Faire Vergleiche**: Packungsgrößen werden erkannt (`750 g`, `6 x 330 ml`, `3er Pack`, `60 Tabs` …). Bei abweichender
  Größe wird über den **Grundpreis** (€/kg, €/l, €/Stk) auf die Joybuy-Packung umgerechnet
- **Aktualisieren-Button** in der UI startet Crawl + Vergleich im Hintergrund (mit Fortschrittsanzeige)
- **Einstellungen** (⚙ oben rechts, beim ersten Besuch automatisch): deine **Postleitzahl** (Marktwahl für REWE) und
  **automatische Aktualisierung** (aus / 6 h / 12 h / täglich / wöchentlich) – läuft, solange `jbc serve` aktiv ist
- Preis-Historie in SQLite – bei jedem Lauf werden Änderungen protokolliert

## Installation

Voraussetzung: Python ≥ 3.12 und [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hendkai/joybuy-compare.git
cd joybuy-compare
uv sync
uv run playwright install chromium
```

## Benutzung

```bash
# 1. Joybuy crawlen + Preise vergleichen (Standard-Suchbegriffsliste, 2 Seiten à 40 Artikel)
uv run jbc run

# eigene Suchbegriffe, max. 100 Vergleiche
uv run jbc run kaffee nutella "usb c kabel" --limit 100

# 2. Web-UI starten → http://127.0.0.1:8787 (fragt beim ersten Besuch nach deiner PLZ)
uv run jbc serve
```

Weitere Befehle:

| Befehl | Zweck |
|---|---|
| `jbc crawl [begriffe…] --pages N` | nur Joybuy-Artikel holen |
| `jbc compare --shops amazon,idealo --limit N --max-age H` | nur vergleichen (Artikel älter als H Stunden) |
| `jbc compare --sku 10135456 --max-age 0` | einen Artikel neu vergleichen |
| `jbc compare --known --max-age 20` | nur bereits verglichene Artikel auffrischen (Preis-Tracking) |
| `jbc rematch` | Matching auf gespeicherten Suchergebnissen neu berechnen – ohne erneutes Scraping |
| `jbc stats` | Kurzstatistik |
| `jbc unblock` | sichtbaren Browser öffnen, falls Joybuy ein Captcha zeigt – einmal lösen, danach läuft es headless weiter |
| `python -m joybuy_compare.debug_shop rewe "Nutella 750 g" 5.79 Nutella` | Matching für einen Shop debuggen |

### Konfiguration (Umgebungsvariablen)

| Variable | Default | Bedeutung |
|---|---|---|
| `JBC_DATA_DIR` | `~/.local/share/joybuy-compare` | Datenbank, Browserprofil, Logs |
| `JBC_ZIP` | – | überschreibt die PLZ, die beim ersten Besuch der Web-UI abgefragt wird (REWE-Preise sind marktabhängig) |
| `JBC_DOH` | `auto` | DNS-over-HTTPS im Scraping-Browser: `auto` = nur wenn das System-DNS Joybuy nicht auflöst (Pi-hole/AdGuard), `1`/`0` = immer/nie |
| `JBC_HEADLESS` | `1` | `0` = Browserfenster anzeigen |

### Täglich automatisch aktualisieren (systemd user timer)

```ini
# ~/.config/systemd/user/joycheck.service
[Service]
Type=oneshot
WorkingDirectory=%h/joybuy-compare
ExecStart=/usr/bin/uv run jbc run --pages 2

# ~/.config/systemd/user/joycheck.timer
[Timer]
OnCalendar=*-*-* 06:30
Persistent=true
[Install]
WantedBy=timers.target
```

`systemctl --user enable --now joycheck.timer`

## Wie es funktioniert

```
joybuy_compare/
├── sources/joybuy.py   Joybuy-Suche (Next.js) → fängt die guide_search_getItems-JSON-Antworten der echten Seite ab
├── sources/shops.py    Amazon / REWE / dm / ALDI SÜD / ALDI Nord / Lidl / idealo (Playwright-DOM, abgefangene JSON-APIs)
├── units.py            Preise, Packungsgrößen, Grundpreise parsen
├── matching.py         Fuzzy-Matching (Marke, Wortabdeckung, Modellnummern, Packungsgröße)
├── crawler.py          Orchestrierung, Throttling pro Shop, Status-Datei für die UI
├── db.py               SQLite (Produkte, Angebote, Suchen, Preisverlauf)
├── server.py           FastAPI-JSON-API
└── static/             Webshop-Frontend (Vanilla JS, kein Build-Schritt)
```

- Joybuy liefert **keine EAN**, daher wird über Titel gematcht: Marke muss vorkommen, Modellnummern (z. B. `WH-1000XM5`)
  müssen übereinstimmen, die Wortabdeckung muss passen. Gleiche Packungsgröße wird bevorzugt, sonst Grundpreis-Vergleich.
- **Tempo**: Pro Shop bleibt ein Browser-Tab geöffnet; Suchen laufen per `fetch()` in diesem Tab (keine Seiten-Renderings,
  0,1–1,5 s pro Suche), alle Shops eines Artikels parallel, identische Suchanfragen werden pro Lauf nur einmal gestellt.
  Gemessen: 40 Artikel in 83 s (vorher ~9 s pro Artikel). Fällt der schnelle Weg aus, wird die Seite klassisch geladen.
- **dm** ist standardmäßig aus: die Such-API lässt nur ca. eine Anfrage pro 6 s zu (sonst Sperre). Aktivieren mit
  `--shops amazon,rewe,aldi_sued,aldi_nord,lidl,idealo,dm`.
- **Joybuy**: pro Suchbegriff wird nur die normale Suchseite geöffnet (~80 Artikel); Blätter-URLs (`&page=`) sind laut
  robots.txt gesperrt und werden nicht genutzt – mehr Abdeckung über mehr Suchbegriffe.
- REWE, dm, ALDI SÜD und ALDI Nord werden nur für Artikel mit Gewichts-/Volumenangabe abgefragt (Lebensmittel/Drogerie).
  ALDI findet nur mit sehr kurzen Suchbegriffen etwas (Marke + 1 Wort, notfalls nur Marke).
- **Lidl**: lidl.de verkauft online fast nur Non-Food, Getränke-Aktionen und Wein – normale Lebensmittelpreise gibt es nur
  in der Lidl-Plus-App/im Prospekt und sind daher nicht vergleichbar.
- ALDI SÜD nutzt die Standardfiliale der Website (das Standardsortiment hat bundesweit einheitliche Preise).
- idealo liefert den günstigsten Händlerpreis („ab …“, ohne Versand). Angebote, die idealo von Joybuy selbst listet
  („Verkauf durch: joybuy.de“), werden verworfen.
- Varianten müssen übereinstimmen (Zero/Light/Bio/Vegan/Vollkorn/Laktosefrei …), ebenso die Produktwörter jenseits der
  Marke („Spaghetti“ ≠ „Fusilli“). Preisunterschiede > Faktor 3,3 gelten als Fehl-Match.
- Ein echter Chromium (Playwright, „new headless“) wird verwendet, da alle Shops Bot-Schutz haben. Zwischen Anfragen an
  denselben Shop wird gedrosselt. Blockt ein Shop (Captcha), wird er für den restlichen Lauf übersprungen.

## ⚠️ Rechtlicher Hinweis / Disclaimer

**Dieses Projekt ist ein privates, nicht-kommerzielles Hobby-/Lernprojekt. Es ist keine Rechtsberatung, und die Nutzung
erfolgt auf eigenes Risiko.** Es besteht keine Verbindung zu Joybuy/JD.com, Amazon, REWE, dm, ALDI SÜD, ALDI Nord, Lidl
oder idealo. Alle Marken gehören ihren jeweiligen Inhabern.

Mehrere der abgefragten Shops **untersagen in ihren AGB/Nutzungsbedingungen ausdrücklich den automatisierten Zugriff**
(Stand der Prüfung: Oktober 2026, Auszüge wörtlich):

| Shop | Regelung zu automatisiertem Zugriff | robots.txt für die genutzten Such-URLs |
|---|---|---|
| **Joybuy** ([AGB 15.5](https://www.joybuy.de/help/terms-conditions)) | ❌ „Keine Person darf Roboter, Spider, Scraper, Data-Mining-Tools oder andere automatisierte Mittel verwenden, um zu irgendeinem Zweck auf Joybuy zuzugreifen, ohne unsere ausdrückliche vorherige schriftliche Genehmigung.“ | `/s?k=…` erlaubt, Blättern (`&page=`) gesperrt |
| **Amazon** ([AGB](https://www.amazon.de/gp/help/customer/display.html?nodeId=201909000)) | ❌ „… kein Data Mining, keine Robots oder ähnliche Datensammel- und Extraktionsprogramme einsetzen, um irgendwelche wesentlichen Teile eines Amazon Services zur Wiederverwendung zu extrahieren“; die Lizenz umfasst keine „Erfassung und Nutzung von Produktinformationen, Beschreibungen oder Preisen“ | Suche `/s` nicht gesperrt |
| **idealo** ([AGB](https://www.idealo.de/legal/agb)) | ❌ „Insbesondere dürfen Nutzende nicht automatisiert auf die Vergleichsportale zugreifen. Dies umfasst vor allem Scraping.“ / „Ein Auslesen oder Ausspähen der Datenbank mittels anderer Software ist nicht gestattet.“ | nicht abrufbar (403) |
| **REWE** ([Nutzungsbedingungen](https://www.rewe.de/service/nutzungsbedingungen/), [AGB](https://www.rewe.de/service/agb/)) | keine ausdrückliche Klausel gefunden (Bedingungen betreffen Kundenkonto, App und Bestellungen) | `?search=` **gesperrt** |
| **dm** ([Nutzungsbedingungen](https://www.dm.de/nutzungsbedingungen-v10-3110528), [AGB](https://www.dm.de/agb-43976)) | keine ausdrückliche Klausel gefunden; die Such-API wird aber technisch stark gedrosselt (daher standardmäßig deaktiviert) | `/search` **gesperrt** |
| **ALDI SÜD** ([Nutzungsbedingungen](https://www.aldi-sued.de/unternehmen/nutzungsbedingungen/neu)) | keine ausdrückliche Klausel gefunden (betrifft das Kundenkonto) | nicht abrufbar (403) |
| **ALDI Nord** ([Impressum](https://www.aldi-nord.de/kundeninformationen/impressum.html)) | keine Nutzungsbedingungen zur Website gefunden | Suche nicht gesperrt |
| **Lidl** ([AGB Onlineshop](https://www.lidl.de/c/allgemeine-geschaeftsbedingungen-onlineshop/s10005181)) | keine ausdrückliche Klausel gefunden (betrifft Bestellungen) | `*search?q=*` **gesperrt** (genutzt wird `/q/api/search`) |

„Keine Klausel gefunden“ heißt nur, dass bei einer automatisierten Stichwortsuche in den verlinkten Texten nichts dazu
gefunden wurde – nicht, dass Scraping dort erlaubt ist. Daneben können z. B. das Datenbankherstellerrecht (§§ 87a ff.
UrhG) und das Umgehen technischer Schutzmaßnahmen (Captchas, Bot-Erkennung) rechtlich relevant sein.

**Wer das Tool nutzt, ist selbst dafür verantwortlich**, die jeweils aktuellen Bedingungen zu prüfen und einzuhalten.
Empfehlungen, falls du es trotzdem privat einsetzt:

- nur für den **eigenen, privaten Preisvergleich** verwenden – keine Veröffentlichung, kein Weiterverkauf, kein öffentliches
  Hosting der gesammelten Daten oder der Web-UI
- **moderat** abfragen (Drosselung nicht entfernen, Auto-Update höchstens täglich bis alle 6 h) und Shops, deren Bedingungen
  du nicht akzeptieren willst, mit `--shops` abwählen
- bei Captchas/Sperren **nicht umgehen**, sondern pausieren
- für kommerzielle Nutzung offizielle Schnittstellen bzw. eine schriftliche Genehmigung einholen (z. B. Amazon
  Product Advertising API)

## Kompletter Import & Preisverlauf

```bash
uv run jbc import          # alle ~580 Joybuy-Kategorien crawlen und alles vergleichen (einmalig, dauert Stunden)
uv run jbc run --kw-limit 150   # 150 am längsten nicht aktualisierte Kategorien + Vergleich (macht auch das Auto-Update)
```

- Die Suchbegriffe kommen aus **Joybuys eigenem Kategoriebaum** (wöchentlich neu geladen) plus der eingebauten Liste;
  eigene Begriffe werden ebenfalls gespeichert. Jeder Lauf nimmt die am längsten nicht abgefragten zuerst – so wird über
  mehrere Läufe der ganze Katalog regelmäßig aktualisiert.
- **Preisverlauf**: Jede Preisänderung (Joybuy und alle Vergleichsshops) landet in `price_history`. Im Shop:
  Filter *Preisentwicklung* (gesunken / gestiegen / bisheriger Tiefstpreis), Sortierung *Größter Preisrückgang* und
  *Am weitesten unter Ø-Preis*, ⭐-Badge für Tiefstpreise, in der Detailansicht eine Tabelle Jetzt / Vorher / Tiefst /
  Höchst / Ø pro Shop.
- Leitet Joybuy auf die Login-Seite um (Soft-Sperre einer „verbrannten“ Session), werden die Joybuy-Cookies automatisch
  gelöscht und es wird neu angesetzt.

## Hinweise

- Matching ist heuristisch – die Detailansicht zeigt Match-Art und -Score, damit man Ausreißer erkennt.
- Preise ohne Versandkosten; Joybuy-Preise können vom Login/Neukunden-Gutscheinen abweichen.

## Lizenz

MIT
