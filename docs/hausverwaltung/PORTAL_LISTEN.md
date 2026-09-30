# Automatische Online-Listen — Mieterliste, Zinsliste, Salden

Auftrag `HV-20260930-PORTAL-LISTEN`, Stand 30.09.2026. Drei geschützte,
rein lesende Seiten im Backoffice (die Telefonpflege in der Mieterakte
ist die einzige Schreibung dieses Auftrags, siehe „Telefon“):

| Seite | Pfad | Parameter |
| --- | --- | --- |
| Mieterliste | `GET /backoffice/mieterliste` | `objekt_id`, `status` (`aktiv` Standard, `beendet`, `alle`), `q` |
| Zinsliste | `GET /backoffice/zinsliste` | `monat` (`YYYY-MM`, Standard: aktueller Monat in Europe/Vienna), `objekt_id`, `q` |
| Salden | `GET /backoffice/salden` | `objekt_id`, `status` (leer = alle, `offen`, `guthaben`, `ausgeglichen`) |

Erreichbar von der Übersicht (Linkzeile „Automatische Listen“, der
aktive Objektfilter wird mitgenommen), von „Mieter & Objekte“, von
„Zahlungen & Mahnungen“ (Salden) und von „Abrechnungen“ (Zinsliste).

Code: `src/mietinkasso/portallisten/service.py` (Berechnung),
`src/mietinkasso/backoffice/routes/listen.py` (Anzeige). Tests:
`tests/mietinkasso/test_portallisten_service.py` und der Abschnitt
„Automatische Online-Listen“ in `tests/mietinkasso/test_backoffice.py`.

## Was „automatisch“ hier heißt

* Jede Seite wird **bei jedem Aufruf neu** aus dem aktuellen
  Datenbankstand berechnet. Es gibt keinen Zwischenspeicher, keinen Job,
  keinen Zeitplan und keinen KI-Aufruf. Gleicher Datenbestand ergibt
  dieselbe Liste.
* Solange der Browser-Tab **sichtbar** ist, lädt die Seite alle 60
  Sekunden neu; Filter und Monat bleiben erhalten (sie stehen in der
  URL). Ein Tab im Hintergrund lädt nicht nach, ebenso wenig, solange
  gerade ein Filterfeld bearbeitet wird.
* Ein GET **schreibt nichts**: keine Buchung, keine Vorschreibung, kein
  Mahnfall, kein Audit-Eintrag, kein Job-Protokoll. Das ist per Test als
  vollständiger Tabellenvergleich vor/nach dem Aufruf abgesichert.
* „Berechnet/angezeigt“ ist der Zeitpunkt des Seitenaufbaus. Er steht
  immer getrennt vom **Datenstand** — insbesondere vom Bankdatenstand
  (siehe unten).

Die bestehenden Abläufe bleiben unverändert: Indexautomatik und
Index-Monatsbericht, Mahnwesen, Vorschreibung und alle Versandschalter
(`SEND_ENABLED` und die getrennten Index-/Vertragsende-Schalter) werden
von diesen Seiten weder gelesen noch gesetzt.

## Zugriff und Geltungsbereich

* Anmeldung wie jede andere Backoffice-Seite über `_current_session`;
  ohne Sitzung Weiterleitung auf `/backoffice/login`, ohne konfiguriertes
  Passwort bleibt alles geschlossen (503). Es gibt keinen öffentlichen
  Endpunkt.
* Gezeigt werden nur Gesellschaften, auf die der Kontext Zugriff hat,
  und nie ein ausgeschlossenes Objekt (107). Ein ausdrücklich
  angefordertes fremdes, unbekanntes oder ausgeschlossenes `objekt_id`
  wird wie in der Rückstandsübersicht mit derselben einheitlichen
  Meldung abgelehnt (HTTP 400).
* Ein Vertrag, dessen eigenes `gesellschaft_id` nicht im Zugriff liegt,
  wird übersprungen, auch wenn sein Objekt erlaubt ist. Ein Debitor
  erscheint nur über seine erlaubten Verträge.

## Mieterliste

* Eine Zeile je Vertrag: Mieter (Debitor), Mietobjekt mit Objektadresse
  und Einheit, Korrespondenzadresse, Telefon, E-Mail, Link zur Akte.
* **Objektadresse** (`Objekt.adresse`) und **Korrespondenzadresse**
  (`Debitor.adresse`) stehen getrennt; die eine wird nie als die andere
  angenommen.
* Fehlende Angaben heißen „Nicht hinterlegt“. Es wird nichts ergänzt.
* Standard („Laufende und künftige“) sind laufende und bereits angelegte
  künftige Mietverhältnisse. „aktiv“ heißt: der Vertrag läuft heute. Ein
  erst später beginnender Vertrag steht als „künftig – beginnt am …“ und
  wird getrennt gezählt, nie als aktiv. Beendete (`gueltig_bis` vor dem
  heutigen Wiener Datum) erscheinen nur unter „Nur beendete“/„Alle“ und
  sind dann als ehemalige Mieter markiert.
* Mieter ist ausschließlich, wer über einen Vertrag als Debitor geführt
  wird — kein Name wird gedeutet, ein technisches Verrechnungskonto wird
  nie zum Mieter.

### Telefon

`debitoren.telefon` ist die einzige Schemaänderung dieses Auftrags:
additiv, nullable, beim Start über `ensure_additive_columns` idempotent
nachgezogen. Bestandszeilen bleiben leer und zeigen „Nicht hinterlegt“.

* **Pflege:** in der Mieterakte unter „Mieter und Objekt“ → „Telefon
  ändern“ (`POST /backoffice/vertrag/{vertrag_id}/telefon`, CSRF,
  Schreibrecht, Gesellschaftszugriff, nicht für ausgeschlossene Objekte).
  Geändert wird ausschließlich die Nummer dieses Mieters; sie gilt für
  alle seine Verträge. Ein leer gespeichertes Feld löscht die Nummer.
  Bei der Neuanlage eines Mieters gibt es ein optionales Telefonfeld.
* **Intake:** optionales Feld `debitoren[].telefon` (siehe
  `IMPORT_VERTRAG.md`). Fehlt es, bleibt eine gespeicherte Nummer
  unverändert — auch bei jedem anderen Upsert ohne Telefonangabe.
* **Prüfung:** höchstens 40 Zeichen, nur Ziffern, Leerzeichen und
  `+ ( ) / - .`, mindestens 3 Ziffern. Es wird nichts umformatiert.
* **Anzeige:** escaped; als `tel:`-Link nur bei eindeutig wählbarer
  Schreibweise. Eine Nummer mit Klammern (z. B. „+43 (0) 660 …“) bleibt
  reiner Text, damit keine falsche Wählfolge entsteht.
* Das Audit-Protokoll vermerkt die Änderung ohne die Nummer selbst.

## Zinsliste

* Eine Zeile je Einheit und im Monat gültigem Vertrag; Einheiten ohne
  Vertrag erscheinen ebenfalls.
* Beträge sind die **gespeicherten Vertragskomponenten, die am
  Monatsersten gelten** — derselbe Stichtag wie im Vorschreibungsentwurf.
  Spalten: Miete (`HMZ`), Küche (`KUECHE`), Stellplatz
  (`PARKPLATZ`/`STELLPLATZ`), BK / Sonstige (jede andere Art), Brutto
  gesamt. Die Seite legt **keine Vorschreibung** an.
* **Netto/USt:** Der gespeicherte Komponentenbetrag ist der Betrag, der
  bei einer Sollstellung gebucht würde (Brutto). Eine Netto-Miete wird
  nur gezeigt, wenn für *alle* Mietkomponenten der Zeile eine geprüfte
  Netto-Mietanteil-Freigabe für den ganzen Monat vorliegt; sonst steht
  „Aufteilung unbekannt“. Es wird nichts aus einem USt-Satz zurückgerechnet.
* **Fehlende Werte:** „–“ = dieser Bestandteil ist nicht hinterlegt.
  „Betrag unbekannt“ = Vertrag gültig, aber keine am Monatsersten gültige
  Komponente. Beides ist nie 0 € und fließt in keine Summe ein; die Zahl
  der Zeilen ohne Betrag wird neben der Summe ausgewiesen. Eine
  ausdrücklich mit 0 gespeicherte Komponente ist dagegen ein echter Betrag.
* **Summen** stammen aus genau den angezeigten (gefilterten) Zeilen.
* **Vertragsbeginn/-ende:** Beginnt der Vertrag erst im Monat, wird kein
  anteiliger Betrag berechnet (Betrag unbekannt); am Mieternamen steht
  „erst ab …“. Endet er im Monat, bleibt der volle Monatsbetrag mit
  Hinweis stehen. Im Folgemonat gibt es keine Vertragszeile mehr.
* **Änderung im Monat („Stand Monatserster“):** Endet der Vertrag oder
  eine gezählte Komponente vor dem Monatsletzten, oder beginnt eine
  Komponente nach dem Monatsersten, ist der gezeigte Betrag nur der
  Stand am Monatsersten. Die Zeile trägt dann die Marke „Stand
  Monatserster – Änderung im Monat, nicht anteilig“ und „prüfen“. Es wird
  nichts aliquotiert und nichts weggelassen: der Betrag bleibt in der
  Summe, Anzahl und Betrag dieser Zeilen stehen aber getrennt daneben.
  Ein Wechsel genau zum Monatsersten ist keine Änderung im Monat.
* **Nutzung** (vermietet, Leerstand, Kurzzeitvermietung, Selfstorage,
  Eigennutzung) ist der aktuell gepflegte `Einheit.nutzungsstatus` —
  nie aus einem Betrag oder Namen abgeleitet. Er hat keine Historie; für
  einen vergangenen Monat wird also der heutige Status gezeigt.
* **Beendeter Vertrag ≠ Leerstand:** Endet ein Vertrag ohne Folgevertrag
  und steht die Einheit weiter auf „vermietet“, zeigt die Liste „prüfen“
  mit dem letzten Vertrag — sie behauptet keinen Leerstand. Erst wenn der
  Nutzungsstatus ausdrücklich auf Leerstand gepflegt ist, steht dort
  Leerstand.
* **Kurzzeitvermietung/Selfstorage** ohne festen Vertrag haben variable
  Erlöse. Die Zeile verlinkt auf die variable Monatsabrechnung und nennt
  nur, ob ein Monatsbericht vorliegt (Entwurf/bestätigt) — dessen Beträge
  werden hier nicht gezeigt und nicht summiert. Gibt es zusätzlich einen
  festen Vertrag, zählt nur dessen Komponente; ein gleichzeitig
  vorhandener Monatsbericht erzeugt einen Prüfhinweis statt einer Addition.

**Grenze Index:** Maßgeblich ist allein die gespeicherte Komponente. Eine
berechnete, aber noch nicht umgesetzte Indexanpassung (fehlende
Rechtsprofil-Freigabe, fehlender Zugangsnachweis, gesperrte oder noch
offene Soll-Umsetzung, deaktivierter Versand) ist **nicht** enthalten.
Der neue Betrag erscheint erst ab dem Monat, in dem die Soll-Umsetzung
eine neue Komponente angelegt hat. Den Stand der Indexfälle zeigt
weiterhin der Index-Monatsbericht, nicht die Zinsliste.

## Salden

* Alle Mietkonten im Geltungsbereich — offen, Guthaben und ausgeglichen —
  sowie Verträge ohne Mietkonto.
* Jede Zahl kommt aus `rueckstaende.service.berechne_rueckstandsuebersicht`
  und damit aus `OPService.berechne_saldo`/`offene_forderungen`. Es gibt
  kein zweites Ledger; die Seite teilt dieselben Werte nur anders auf:

  | Spalte | Herkunft |
  | --- | --- |
  | Kontosaldo offen | positiver Kontostand |
  | Fällig | offene Einzelposten mit bekannter, verstrichener Fälligkeit |
  | Noch nicht fällig | offene Einzelposten mit bekannter, künftiger Fälligkeit |
  | Fälligkeit unbekannt | offene Einzelposten ohne erfasstes Fälligkeitsdatum |
  | Guthaben | negativer Kontostand, als positiver Betrag |

* Guthaben und Rückstand werden je Konto getrennt geführt. Auch die
  Summenzeile verrechnet das Guthaben eines Mieters nie mit dem Rückstand
  eines anderen.
* **Kein Mietkonto** heißt „Saldo unbekannt“, nicht „ausgeglichen“. Der
  Statusfilter „ausgeglichen“ enthält solche Zeilen nicht.
* Eine Mahnsperre bleibt als Hinweis sichtbar und ändert keinen Betrag.
  Die Seite plant und versendet keine Mahnung.
* „Fällig“, „Noch nicht fällig“ und „Fälligkeit unbekannt“ sind Summen
  der offenen **Einzelposten** und werden eigenständig ermittelt. Sie
  sind keine Aufteilung des Kontosaldos („davon“) und müssen sich nicht
  zu ihm addieren. Weichen Kontostand und Summe der offenen Einzelposten
  voneinander ab (z. B. nach einer Korrekturbuchung), steht ein Hinweis
  „Kontoabweichung“ mit dem Betrag in der Zeile; die Einzelheiten zeigt
  die Akte bzw. das Konto.

### Bankdatenstand

Drei Zeitangaben stehen getrennt und bedeuten Verschiedenes:

* **Kontoauszüge lückenlos bestätigt bis** — die ausdrückliche
  Bestätigung unter „Bankvollständigkeit“.
* **Letzte importierte Buchung vom** — das jüngste Buchungsdatum einer
  eingespielten Bankzeile (kein Vollständigkeitsnachweis).
* **Berechnet/angezeigt** — der Zeitpunkt des Seitenaufbaus; er sagt
  nichts über die Bankdaten. „Buchungen bis“ ist der Stichtag der
  Saldorechnung.

Über der Mietertabelle steht dazu eine kurze Zeile „Bank: …“ (bei
mehreren Bankkonten das am wenigsten weit bestätigte Datum und das
jüngste Importdatum) mit sichtbarer Warnung, wenn ein Bankkonto fehlt,
nie bestätigt wurde oder keine Bankzeile importiert ist. Die Tabelle je
Bankkonto steht eingeklappt unter der Mietertabelle („Bankdatenstand je
Bankkonto“). Ohne hinterlegtes Bankkonto steht dort „Kein Bankkonto
hinterlegt“, ohne Bestätigung „nie bestätigt“.

**Grenze Bank:** Es gibt keinen automatischen Bankabruf (EBS/EBICS
zurückgestellt). Zahlungen erscheinen erst, nachdem eine Bankdatei
eingelesen und zugeordnet wurde. Ein offener Saldo kann deshalb eine
bereits geleistete, noch nicht eingespielte Zahlung enthalten; die
Anzeigezeit der Seite sagt darüber nichts. Ein Bankkonto gehört einer
Gesellschaft, nicht einem einzelnen Mietkonto — der Bankstand gilt daher
für alle Mietkonten dieser Gesellschaft gemeinsam.
