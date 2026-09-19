# Backoffice — Modulaufbau und Grenzen

Auftrag `HV-20260919-CODEQUALITAET`, Stand 19.09.2026. Die bisherige
Sammeldatei `src/mietinkasso/backoffice/app.py` mit 5343 Zeilen und 89 Routen
ist in 14 fachliche Routenmodule sowie gemeinsame Abhängigkeiten und
Sicherheitsfunktionen aufgeteilt. Der öffentliche Einstieg bleibt `router`.
Das einmalige Umbauwerkzeug ist als Arbeitsnachweis außerhalb des
Repositories archiviert und gehört nicht zur Anwendung.

Die Zerlegung ist ausdrücklich eine **Struktur**-Änderung. Fachregeln,
Berechnungen, Ausgaben, URLs, Formularfelder, Statuscodes und die
serverseitigen Sperren (Login/CSRF, Objekt-107-Ausschluss, Schreibschutz,
gebundene Vorschau→Apply-Vorgänge) bleiben unverändert; der Quelltext der
Routen wurde mechanisch verschoben, nicht neu formuliert.

## Schichten

```
api/app.py
  └── backoffice/app.py            Router-Komposition, sonst nichts
        ├── backoffice/routes/*    fachliche Routenmodule (je ein prefixloser APIRouter)
        │     ├── backoffice/auth.py          Login/Session/CSRF/Scope/Layout
        │     ├── backoffice/routes/shared.py bereichsübergreifende Helfer
        │     └── backoffice/dependencies.py  genau EINE Instanz je Abhängigkeit
        └── (backoffice/views.py, security.py, *_form.py — unverändert)
```

Die Pfeile gehen nur nach unten. Kein Modul unterhalb von `app.py`
importiert `app.py` zurück, kein Routenmodul importiert ein anderes
Routenmodul. Es gibt keine Kompatibilitätsfassade, keine
Wildcard-Importe, keine Import- oder `exec`-Magie.

## Verantwortungen

| Modul | Verantwortung | ausdrücklich NICHT |
| --- | --- | --- |
| `app.py` | erzeugt den `/backoffice`-Router und bindet die Teil-Router in einer kommentierten, festgelegten Reihenfolge ein | keine Route, kein HTML, keine Abhängigkeit |
| `dependencies.py` | Konfiguration, Session-Factory, Repositories/Services, `SessionStore`, `LoginRateLimiter`, Cookie-Name, Umgebungs-Kennzeichen | keine Route, kein HTML, keine Fachlogik |
| `auth.py` | `_require_enabled`, `_current_session`, `_ctx`, `_verify_csrf`, Origin-Prüfung, `_layout`/`_fehlerseite`, Objekt-Sperrprüfung | keine Route (die Login-Routen liegen in `routes/auth.py`) |
| `routes/shared.py` | nur, was nachweislich mehrere Fachbereiche brauchen (derzeit die Auswahlliste der Rechtsordnungen) | kein Router, kein Sammelbecken |
| `routes/auth.py` | Anmeldung, Abmeldung | Sitzungsmechanik (die liegt in `security.py`/`auth.py`) |
| `routes/dashboard.py` | Rückstandsübersicht, drei Bereichs-Startseiten | eigene Berechnung — alles kommt aus `rueckstaende.service` |
| `routes/konten.py` | Kontoauszug, Nachbuchung, Storno/Korrektur, Eröffnungsimport, Vorschreibungsentwurf/Sollstellung | Bankzuordnung, Mahnwesen |
| `routes/bank.py` | Bankdatei-Import/Vorschau, Zuordnung (automatisch/manuell/verknüpfen), Bankvollständigkeit | Mahnfreigabe-Ableitung (die liegt beim Mahnwesen) |
| `routes/mahnwesen.py` | Mahnvorschau, ausdrückliches Planen, Mahnbrief-PDF, Sendebereitschaft, Versand, Mahnstufen-Konfiguration | Zinsprofilpflege, Mailnachweise |
| `routes/zinsprofile.py` | Zinsprofil je Vertrag, OeNB-Basiszinssatz | Mahnkostenberechnung selbst |
| `routes/mailversand.py` | Versandübersicht und Nachweisabgleich | eigener Versand |
| `routes/vertragspruefung.py` | Rechtsordnungs-Prüfversionen, Sperren, Index-Prüfbedarf | Indexklausel/Rechtsprofil (siehe `indexprofile`) |
| `routes/mieweg.py` | MieWeG-2026-Berechnungsvorschau | Vorschreibung, Freigabe, Versand |
| `routes/indexprofile.py` | vertragsbezogene Indexklauseln und Rechtsprofile (Entwurf → Freigabe) | laufender Monatsbetrieb |
| `routes/indexbetrieb.py` | Outbox, Monatsläufe, Soll-Umsetzung, VPI-Pflege, Vertragsende-Erinnerungen | der Monats-/Tageslauf selbst (läuft über `scripts/indexautomatik_*.py`, nicht über HTTP) |
| `routes/monatsbericht.py` | Index-Monatsbericht (Owner), Liste und Detail | zweite/eigene Berechnung |
| `routes/variableabrechnung.py` | variable Monatsabrechnung, CSV-Import, Versionen, Netto-Monatsübersicht, Netto-Mietanteil-Freigabe | Bank-Ist |
| `routes/vertragsanlage.py` | Vertragsliste, Mieterakte, Vertragsanlage/-bearbeitung über die Intake-Strecke | zweite Buchungsstrecke, Original-PDFs in der Datenbank |

## Zwei Regeln, die nicht verhandelbar sind

**1. Abhängigkeiten über Modulzugriff, nicht über kopierte Importe.**
Routen lesen `deps._stammdaten_repo`, `deps._settings`,
`deps._DEMO_UMGEBUNG` usw. — nie `from ...dependencies import
_stammdaten_repo`. Ein kopierter Import würde den Wert beim Import
einfrieren; ein zur Laufzeit gesetzter Wert (Tests setzen
`_DEMO_UMGEBUNG`, um den Echtbetriebs-Zweig der automatischen
Bankzuordnung zu prüfen) wirkte dann nicht mehr an der Stelle, an der die
Route ihn liest. Genauso wichtig: es gibt genau einen `SessionStore`,
einen `LoginRateLimiter` und einen Servicegraph — eine zweite Instanz
würde Sitzungen und Sperrfenster still aufspalten.

**2. `_current_session` ist EIN Funktionsobjekt.**
Jede geschützte Route verwendet `Depends(_current_session)` aus
`backoffice/auth.py`. Würde ein Modul die Funktion neu definieren,
entstünden mehrere unabhängige Abhängigkeitsbäume mit eigenem
Cookie-Alias.

## Reihenfolge der Routen

Starlette prüft Routen in Registrierungsreihenfolge. `app.py` legt sie
darum ausdrücklich und kommentiert fest, statt sie aus Importzufällen
entstehen zu lassen. Zwei Stellen sind dabei inhaltlich relevant:

* `GET /backoffice/vertrag/weiterleiten` muss vor
  `GET /backoffice/vertrag/{vertrag_id}` stehen — beide liegen in
  `routes/vertragsanlage.py` und dürfen dort nicht getrennt werden.
* `routes/vertragsanlage.py` wird zuletzt eingebunden, weil es den
  einzigen zweisegmentigen Platzhalter unter `/vertrag/` enthält.

Die fachliche Gruppierung verschiebt Registrierungspositionen, unter anderem
Vorschreibungs-, Bank-Verknüpfungs- und Mahnpolicy-Routen. Die Menge aus
Methode, Pfad und Endpunktname bleibt gleich. Ein unabhängiger Vergleich
prüft zusätzlich die tatsächlich aufgelösten Endpunkte und ihre
Authentifizierungsabhängigkeiten. Der statische Weiterleitungspfad bleibt
vor dem Vertragsplatzhalter registriert.

## Prüfung

- Vorher: 1058 Tests erfolgreich; nachher: 1061 Tests erfolgreich.
- Drei neue HTTP-Vertragstests laufen in einem eigenen Prozess mit eigener
  Testkonfiguration. Sie importieren die Anwendung nicht während der
  Testsammlung und beeinflussen andere Tests deshalb nicht.
- Vollständiges OpenAPI-Schema mit 94 Operationen sowie alle 89
  Backoffice-Routen einschließlich unauthentifizierter Antworten unverändert.
- AST-Vergleich aller 138 ursprünglichen Funktionen identisch nach
  Normalisierung ausschließlich des neuen `deps.`-Modulzugriffs.
- Alle 544 Assertions im bestehenden Backoffice-Testmodul erhalten;
  sämtliche anderen 173 bestehenden Quelldateien unverändert.
- Gesamtlauf in isoliertem Linux-Container aus dem vorhandenen Runtime-Image,
  ohne Netzwerk, ohne Produktivdaten/-Volumes und mit schreibgeschütztem Code.
  Die zwei bestehenden Deprecation-Warnungen sind unverändert.

Das ist eine Entwicklungsabnahme. Der Branch wurde damit nicht produktiv
bereitgestellt. Die Initialisierung und langen einzelnen HTML-Funktionen
bleiben die nachfolgend beschriebenen Grenzen.

## Was diese Zerlegung NICHT löst

* **Modulebenen-Initialisierung beim Import.** `dependencies.py` baut
  beim Import Engine, Repositories und Services auf — wie bisher
  `app.py`. Der Auftrag bündelt das an einer Stelle und macht es
  sichtbar; er ersetzt es nicht durch eine echte
  Dependency-Injection/Lifespan-Initialisierung. Ein Import des
  Backoffice bleibt damit an eine konfigurierte Datenbank gekoppelt.
* **`vertragsanlage_form.py` (1064 Zeilen)** bleibt unberührt und
  oberhalb der 1000-Zeilen-Grenze; dieser Auftrag betrifft nur die
  Routenschicht.
* **`_ist_objekt_gesperrt`** in `auth.py` hat keinen Aufrufer mehr. Der
  Code wurde bewusst nicht gelöscht, weil dieser Auftrag Verhalten und
  Oberfläche unverändert lassen soll; das Entfernen ist eine eigene,
  kleine Entscheidung.
* **Datei-`/`Routengrößen.** Die Routenmodule liegen deutlich unter 1000
  Zeilen, einzelne Routenfunktionen (u. a. `vertragsanlage_detail`,
  `bank_unzugeordnet`, `rechtsprofil_uebersicht`) bleiben aber lang, weil
  ihr HTML unverändert erhalten bleiben musste.
