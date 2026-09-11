# Review-Zusammenfassung — Mietinkasso-Modul

Stand: zweite Runde nach unabhängiger Code-Abnahme durch Codex.
"Lieferung" heißt hier: alle in RAHMENPROGRAMM.md geforderten Bausteine
sind implementiert und getestet; rechtlich/fachlich heikle Bereiche
(Index, BK-Umlageschlüssel, Mahntexte) sind bewusst als versionierte,
geprüft freizugebende Regelprofile gebaut statt als vermeintlich fertige
Wahrheit — Details dazu in `OFFENE_PUNKTE.md`.

## Korrekturrunde nach Codex-Abnahme (HEAD 38e8895 geprüft)

Codex hat den ersten fertigen Stand (Commit `38e8895`) unabhängig
gegengeprüft: die 147 vorhandenen Tests liefen grün, aber 8 zusätzliche
fachliche Gegenproben schlugen fehl, plus mehrere weitere
Abnahmesperren beim Code-Lesen. Alle sind in dieser Runde behoben, mit
neuen Regressionstests belegt:

1. **`claim_fuer_versand` war nicht exklusiv** — zwei aufeinanderfolgende
   Aufrufe lieferten beide `True`, weil nur ein Zeitstempel gesetzt
   wurde, der Status aber `GEPLANT` blieb. Jetzt ein atomarer
   Compare-and-Swap `GEPLANT -> IN_VERSAND`; ein Absturz zwischen Claim
   und Ergebnis bleibt `IN_VERSAND` und wird nur über die explizite
   Recovery-Funktion `markiere_verwaiste_als_unsicher()` aufgelöst (nie
   automatisch erneut versucht). Siehe `test_claim_fuer_versand_ist_exklusiv`,
   `test_verwaiste_in_versand_faelle_werden_nicht_automatisch_erneut_versucht`.
2. **`planen` ignorierte Policy-Freigabe und Wartefrist** — eine
   `ENTWURF`-Policy wurde akzeptiert, und Stufe 1 wurde schon am
   Fälligkeitstag geplant statt erst `stufe1_tage_nach_faelligkeit` Tage
   danach. Jetzt: `PolicyNichtFreigegebenError` bei nicht freigegebener
   Policy, neuer Status `ZU_FRUEH` vor Ablauf der Frist. Siehe
   `test_entwurf_policy_darf_nichts_planen`,
   `test_stufe1_erst_nach_wartefrist_nicht_schon_am_faelligkeitstag`.
3. **Eröffnung ließ sich mit zweiter `import_id` verdoppeln** — Eröffnung
   ist jetzt fachlich (Anwendungslogik) UND per partiellem Unique-Index
   auf DB-Ebene (`uq_op_eroeffnung_pro_konto`) einmalig je Konto; ein
   Replay mit identischem Betrag/Stichtag ist ein No-Op, ein
   widersprüchlicher Zweitimport wird blockiert. Siehe
   `test_eroeffnung_gesamtsaldo_gleicher_fakt_mit_anderer_import_id_ist_replay`,
   `test_eroeffnung_gesamtsaldo_widersprechender_zweitimport_wird_blockiert`.
4. **`zuordnen_manuell` akzeptierte jeden Betrag** — 1000 € Zuordnung aus
   einer 600 €-Zahlung wurde anstandslos gebucht. Jetzt validiert
   `bank/repository.py::create_zuordnung` (und die service-seitige
   Vorabprüfung) Betrag > 0, Währungsgleichheit und verbleibenden
   Restbetrag der Transaktion; Teilzuordnungen lassen den Rest sichtbar
   unzugeordnet. Siehe `test_zuordnen_manuell_lehnt_betrag_ueber_transaktionshoehe_ab`,
   `test_zuordnen_manuell_teilzuordnung_laesst_rest_unzugeordnet`.
5. **Cross-Tenant-Buchung hinterließ eine Phantom-Zahlung** — die
   OP-Zeile wurde gebucht, BEVOR die Zuordnung an der
   Gesellschaftsprüfung scheiterte. Jetzt validiert
   `BankImportService._pruefe_vor_buchung` alles (Gesellschaft, Betrag,
   Währung, Restbetrag) VOR jedem `op_service.buchen()`-Aufruf; die
   Rücklastschrift prüft zusätzlich Original-Zuordnung, Konto-Zugehörigkeit,
   Typ/Vorzeichen und erlaubt einen begrenzten Teilbetrag. Siehe
   `test_cross_tenant_zuordnung_hat_keine_op_nebenwirkung_ueber_service`.
6. **Überlappende Bank-Exports ohne native ID verdoppelten Zahlungen** —
   die `import_id` schloss die Zeilennummer ein, die zwischen
   überlappenden Exports variiert. Jetzt: mit bankseitig eindeutiger
   Kennung (CAMT `AcctSvcrRef`, CSV-Spalte `eindeutige_referenz`, beide
   je Bankkonto gescopt) ist ein Replay ein sauberer No-Op; OHNE eine
   solche Kennung löst ein wirtschaftlich identischer Fingerprint einen
   `MehrfachbuchungsKonfliktError` aus statt stiller Verdopplung oder
   stiller Zusammenlegung. CAMT zusätzlich gehärtet: formatierungs-
   unabhängiger Inhalts-Hash (kein `ImportConflictError` durch
   XML-Whitespace-Änderungen), Sammelbuchungen mit mehreren `TxDtls`
   werden nur bei exakt aufsummierenden Teilbeträgen automatisch
   aufgeteilt (sonst `CamtMehrteiligeBuchungError`), fehlende
   Pflichtfelder (`Amt`, `BookgDt`, `CdtDbtInd`) und Fremdwährungen
   brechen den Import ab statt eine Zeile still zu überspringen. Siehe
   `test_csv_ueberlappung_ohne_native_id_ist_konflikt_statt_doppelimport` und
   die weiteren `test_camt053_*`-Tests in `tests/mietinkasso/test_bank.py`.
7. **Index-Schwelle unterdrückte Senkungen; Dämpfung war ein harter
   Deckel** — `Basis100/neu95/Schwelle3/HMZ500€` ergab 0 statt -25 €,
   weil die Schwelle gerichtet statt auf den Betrag der Veränderung
   geprüft wurde. Dämpfung deckelte hart bei `daempfung_prozent`, statt
   "Schwelle plus Hälfte des darüberliegenden Anstiegs" zu rechnen.
   Beides korrigiert (Schwelle: `abs(veraenderung) >= schwelle`,
   inklusive Grenzfall; Dämpfung: `schwelle + (überschuss / 2)`). Nicht
   implementierte Rechtsprofile (April-Termine,
   Jahresdurchschnittsbildung, anteilige Erstvalorisierung,
   Altvertragsübergang) sind jetzt über ein Pflichtfeld
   `berechnungsprofil` explizit gesperrt (`RechtsprofilNichtImplementiertError`),
   statt von der generischen Formel überspielt zu werden. Siehe
   `test_schwelle_wirkt_auf_betrag_der_veraenderung_auch_bei_senkung`,
   `test_gesetzliche_daempfung_ist_schwelle_plus_haelfte_des_ueberschusses`,
   `test_nicht_implementiertes_rechtsprofil_wird_gesperrt`.

Weitere beim Lesen gefundene Abnahmesperren, ebenfalls behoben:

- **Mahnstufen galten pro Vertrag statt pro Forderung** — nach Stufe 2
  einer alten Forderung verhinderte das jede neue Stufe 1 für einen
  späteren, unabhängigen Rückstand. Das Mahnwesen ist jetzt komplett auf
  Forderungsebene umgebaut: `op_service.offene_forderungen()` liefert
  jede offene Forderung (FIFO-Zahlungszuordnung, älteste zuerst) mit
  eigenem Reststand; `MahnFallTable.forderung_op_position_id` bindet den
  Stufe1->Stufe2-Zyklus an genau diese Forderung. Siehe
  `test_neue_forderung_bekommt_eigenen_zyklus_nach_stufe2_der_alten`.
- **`versenden` prüfte weder frische Bankvollständigkeit noch
  Forderungsidentität neu, und LESEN durfte versenden** — `versenden`
  lädt Vertrag/Konto jetzt serverseitig über den gespeicherten
  `MahnFall` neu (nie die Aufrufer-Objekte), verlangt `require_schreibrecht`
  (fehlte zuvor komplett), prüft Bankfrische UNMITTELBAR vor dem Versand
  erneut und vergleicht nicht nur die Kontosumme, sondern ob GENAU DIESE
  Forderung noch in mindestens der geplanten Höhe offen ist (fängt auch
  "andere Änderung, zufällig gleiche Summe"). Siehe
  `test_bankstand_veraltet_bei_versand_stoppt_auch_wenn_bei_planung_frisch`,
  `test_lesezugriff_darf_nicht_planen_oder_versenden`.
- **Unauthentifizierte OP-/Outbox-GETs gaben Daten aus** —
  `api/app.py` ist jetzt "closed by default": ohne konfigurierten
  `MIETINKASSO_API_TOKEN` antworten die Datenendpunkte mit 503, mit
  Token ist ein `X-API-Key`-Header Pflicht. Das ist weiterhin KEINE
  echte Mandantentrennung pro Endanwender (nur ein geteilter
  Operator-Token) — siehe `OFFENE_PUNKTE.md`.
- **`dokument_zustellen`/`hauptbuch_exportieren` erfanden Erfolgsstatus** —
  beide verlangen jetzt einen tatsächlich übergebenen, nicht-leeren
  Nachweis (`zustellnachweis`/`export_nachweis`) und lehnen sonst mit
  `NachweisFehltError` ab; ein Export ist erst nach `ZUGESTELLT` möglich
  (vorher fehlte diese Statusprüfung komplett). Siehe
  `test_dokument_zustellung_und_export_verlangen_echten_nachweis_und_reihenfolge`.
- **Index/BK hatten keine Auth-Prüfung** — jede Gesellschaft konnte für
  jeden fremden Vertrag/jedes fremde Objekt Klauseln/Abrechnungen
  anlegen. Beide Services verlangen jetzt `ctx: AuthContext` mit
  `require_gesellschaft_access`/`require_schreibrecht` auf jeder
  schreibenden Methode. Siehe `test_index_ist_fremder_gesellschaft_nicht_zugaenglich`,
  `test_bk_ist_fremder_gesellschaft_nicht_zugaenglich`.
- **Vertrags-/Konto-Fehlzuordnung wurde nicht geprüft** — `BKService.ergebnisse_buchen`
  und `VorschreibungService.sollstellen` akzeptierten ein `Konto`, das
  zu einem ANDEREN Vertrag gehört, ungeprüft. Beide lehnen das jetzt mit
  `BindungInkonsistentError` ab. Siehe
  `test_bk_buchung_mit_falsch_zugeordnetem_konto_wird_abgelehnt`,
  `test_sollstellen_lehnt_falsch_zugeordnetes_konto_ab`.
- **Unveränderliche Komponentenstände** — `VertragsKomponenteTable` hat
  ohnehin keine Update-Methode (nur Insert); ein expliziter Test belegt
  jetzt, dass eine zweite Buchung mit gleicher ID einen DB-Fehler statt
  eines stillen Overwrites auslöst. Siehe
  `test_vertragskomponente_ist_unveraenderlich`.

**Bekannte Restlücke (Stand Runde 1, in Runde 2 behoben):** die oben
beschriebene Vorab-Validierung schloss die GEMELDETEN, deterministischen
Fehlerfälle vollständig, aber OP-Buchung und Zuordnungserstellung liefen
noch in zwei getrennten DB-Transaktionen. Genau dieses Fenster hat
Codex' zweite Rückprüfung als reproduzierbaren Bug demonstriert (siehe
"Korrekturrunde 2" unten) — inzwischen behoben.

## Korrekturrunde 2 nach zweiter Codex-Rückprüfung (HEAD 048da6b geprüft)

Codex hat den nach Runde 1 gepushten Stand (Commit `048da6b`) erneut
unabhängig gegengeprüft: 175 Repo-Tests liefen grün und die 8
ursprünglichen Gegenproben aus Runde 1 bestanden ebenfalls, aber zwei
weitere, unabhängige Gegenproben fanden reproduzierbare Kernfehler, plus
mehrere gezielte Restpunkte aus dem Quellcode und ein zusätzlicher
fachlicher Befund zur Objekt-107-Ausschlussregel. Alle sind in dieser
Runde behoben, mit neuen Regressionstests belegt:

1. **Bug 1 — kein Rollback bei Fehler NACH der OP-Buchung:** Codex ließ
   `bank_repo.create_zuordnung` nach erfolgreichem `OPService.buchen`
   einen simulierten `RuntimeError` werfen; der Kontosaldo blieb bei
   -60000 stehen, statt auf 0 zurückzurollen — reproduzierbar bereits in
   einem einzelnen Prozess, kein nur theoretisches
   Parallelitätsproblem. Behoben, indem OP-Buchung, Zuordnung und
   Audit-Eintrag jetzt in EINER gemeinsamen DB-Transaktion laufen
   (`BankImportService._zuordnen_atomar`, ebenso `_importiere_atomar`
   für Datei-Importe): jeder Fehler — auch ein unerwarteter — rollt die
   GESAMTE Transaktion zurück; nichts Teilweises bleibt hängen. Konto,
   Transaktion und Original-OP werden dabei serverseitig frisch aus der
   DB geladen. Siehe `test_zuordnung_fehler_nach_op_buchung_rollt_alles_zurueck`
   in `tests/mietinkasso/test_bank.py`. Verbleibende, ehrlich benannte
   Restlücke bei echter Nebenläufigkeit unter einer produktiven
   Mehrbenutzer-DB: `OFFENE_PUNKTE.md`.
2. **Bug 2 — dieselbe Zahlungstransaktion ließ sich als eigene
   Rücklastschrift durchschmuggeln:** derselbe positive 60000-Cent-
   Bankeingang wurde zuerst als Zahlung zugeordnet und dann unverändert
   als `transaktion` an `verarbeite_ruecklastschrift` übergeben — er
   wurde akzeptiert, der Saldo stieg von -60000 auf 0. Die Methode
   validierte bislang nur den Zustand der URSPRÜNGLICHEN Zahlung, nie
   die tatsächliche Beschaffenheit der übergebenen `transaktion`.
   `verarbeite_ruecklastschrift` verlangt jetzt: einen tatsächlich
   negativen Bankeingang, ein zur Ursprungszahlung passendes Bankkonto
   (über deren Zuordnungskette ermittelt), übereinstimmende
   Gesellschaft/Währung, eine kumulative Rückbuchungsgrenze der
   Ursprungszahlung (`kumulativ_zurueckgebucht`, nie mehr zurückbuchen
   als ursprünglich bezahlt, auch nicht über mehrere
   Teil-Rücklastschriften) sowie einen begrenzten verfügbaren
   Belastungsbetrag der Rücklastschrift-Transaktion selbst
   (`verwendeter_betrag_rueckbuchung`, damit eine Sammel-Rücklastschrift
   mehrere Ursprungszahlungen abdecken kann, aber nie mehr als ihren
   eigenen Betrag). Siehe `test_ruecklastschrift_lehnt_dieselbe_positive_transaktion_ab`,
   `test_ruecklastschrift_lehnt_falsches_bankkonto_ab`,
   `test_ruecklastschrift_kumulative_ruecklastgrenze_der_ursprungszahlung`,
   `test_ruecklastschrift_verfuegbarer_belastungsbetrag_der_transaktion`.
3. **Retries vs. echte Teilzuordnungen mit identischem Betrag nicht
   unterscheidbar:** die bisherige Idempotenz hing an
   `(bank_transaktion_id, op_position_id)`, was zwei genuine, unabhängige
   Teilzuordnungen mit zufällig gleichem Betrag technisch unmöglich
   machte. `ZuordnungTable` hat jetzt eine vom Aufrufer explizit
   vergebene, eindeutige `vorgang_id` als alleinigen Idempotenzschlüssel:
   derselbe Wert (Retry, z. B. nach Netzwerk-Timeout) ist ein sicherer
   No-Op, ein neuer Wert erzeugt garantiert eine eigene Buchung.
   `zuordnen_manuell` verlangt `vorgang_id` jetzt als Pflichtparameter.
   Siehe `test_vorgang_id_unterscheidet_retry_von_unabhaengiger_teilzuordnung`.
4. **Datei-Importe konnten unsichtbare Teilergebnisse hinterlassen:**
   CAMT.053-/CSV-Importe laufen jetzt vollständig atomar je Aufruf
   (`_importiere_atomar`, eine Session/Transaktion für die gesamte
   Datei): scheitert eine Zeile, wird der GESAMTE Lauf zurückgerollt und
   der ursprüngliche Fehlertyp (nicht verschluckt, nur um Zeilenkontext
   angereichert) weitergereicht; ein erneuter, korrigierter Lauf ist
   dank Idempotenz immer gefahrlos möglich.
5. **Index kannte nur eine inklusive Schwelle:** viele reale Verträge
   verlangen strikt "über 3 %"/"über 2 %" (exklusiv), nicht "ab 3 %"
   (inklusiv). `IndexKlauselTable` hat jetzt ein explizites
   `schwelle_inklusive: bool`-Feld (Default `True`, rückwärtskompatibel);
   `IndexService` prüft je nach Flag `<` oder `<=` gegen die Schwelle und
   nimmt den Wert in den Snapshot auf. Grenzfall-Tests für beide
   Varianten in `tests/mietinkasso/test_index.py`.
6. **Bankfrische aus dem letzten Buchungsdatum bewies keine
   Exportvollständigkeit:** `bankstand_alter_tage` zeigt nur das Datum
   der letzten IRGENDEINER importierten Zeile — ein lückenhafter Import
   kann trotzdem aktuell aussehen. Neue `BankVollstaendigkeitTable` plus
   `bestaetige_bankvollstaendigkeit`/`bankvollstaendigkeit_bestaetigt_bis`
   bilden eine EXPLIZITE menschliche/prozessuale Bestätigung
   "lückenlos bis Datum X" ab; das Mahnwesen (`plane_forderung`,
   `versenden`) verlangt jetzt `bank_bestaetigt_bis` statt eines reinen
   Alters-Grenzwerts und blockiert zusätzlich bei relevanten,
   auf den Vertrag referenzierten, aber noch unvollständig zugeordneten
   Bankeingängen (`hat_ungeklaerte_relevante_eingaenge`). Siehe
   `test_fehlende_bankbestaetigung_blockiert_mahnung`,
   `test_ungeklaerte_eingaenge_blockieren_mahnung`,
   `test_ungeklaerte_eingaenge_stoppen_versand`.
7. **`versenden` prüfte Policy und Empfänger nicht frisch:** zwischen
   Planung und Versand konnte die Policy zurückgezogen oder der
   Empfänger entfernt werden, ohne dass der Versand das bemerkte.
   `versenden` lädt jetzt die aktuell freigegebene Policy und den
   Debitor samt E-Mail-Adresse UNMITTELBAR vor dem Versand neu und
   blockiert bei Policy-Wechsel bzw. fehlendem Empfänger. Siehe
   `test_policy_wechsel_zwischen_planung_und_versand_stoppt`,
   `test_empfaenger_entfernt_zwischen_planung_und_versand_stoppt`,
   `test_fehlender_empfaenger_blockiert_mahnung`.
8. **Objekt-107-Ausschluss war nur eine isolierte, nie aufgerufene
   Hilfsfunktion:** Codex' Gegenprobe legte Objekt 107 mit
   `ausgeschlossen=True` an und buchte über
   `OPService.eroeffnen_gesamtsaldo(50000)` trotzdem 500 € — auch als
   ADMIN. `pruefe_objekt_erlaubt` wurde von keinem buchenden Service
   aufgerufen. Der Ausschluss wird jetzt zentral und rollenunabhängig
   (ADMIN eingeschlossen) aus dem PERSISTIERTEN Konto→Vertrag→
   Einheit→Objekt aufgelöst (`StammdatenRepository.objekt_fuer_vertrag`/
   `pruefe_vertrag_nicht_ausgeschlossen`/`pruefe_konto_nicht_ausgeschlossen`,
   frisch aus der DB, nicht aus vom Aufrufer übergebenen Objekten) und in
   JEDEN schreibenden Finanzpfad verdrahtet: `OPService.buchen`/
   `eroeffnen_gesamtsaldo`/`eroeffnen_einzel_op` (deckt damit
   transitiv auch Bankzuordnung und BK-Buchung ab, weil beide über
   `op_service.buchen` laufen), `VorschreibungService.entwurf_erstellen`,
   `IndexService.klausel_anlegen`/`berechne_vorschlag`,
   `BKService.abrechnung_anlegen`, `MahnwesenService.plane_forderung`/
   `versenden`. Siehe `test_objekt_107_wird_auch_im_mahnwesen_ausgeschlossen`
   sowie die `ObjektAusgeschlossenError`-Tests in `test_stammdaten.py`,
   `test_index.py`, `test_bk.py`, `test_vorschreibung.py`.

`OFFENE_PUNKTE.md` wurde entsprechend korrigiert: die dort zuvor
enthaltene, widersprüchliche Aussage, MieWeG-2026-Detailregeln seien
"als Parameter der Klausel abbildbar", ist ersetzt durch die korrekte
Aussage, dass NUR `EINFACHER_SCHWELLENVERGLEICH` implementiert ist und
jedes andere `berechnungsprofil` technisch mit
`RechtsprofilNichtImplementiertError` gesperrt bleibt.

## Repo-Befund (vor Implementierung)

- Stack: Python 3.11, FastAPI, SQLAlchemy 2.0, Pydantic v2, pytest,
  SQLite. Kein AGENTS.md/CLAUDE.md vorhanden (jetzt ergänzt).
- `src/invoice_automation/` ist ein Kreditoren-/Eingangsrechnungs-
  Scaffold (OCR → Validierung → Duplikatserkennung → Zahlungs-XML,
  Migration von Zapier). Es gibt dort **keine** Debitoren-, OP-,
  Bank-, Mahnwesen- oder Vertragslogik — ein anderer Bounded Context
  (Kreditoren/AP) als der beauftragte (Debitoren/AR, Hausverwaltung).
- Entscheidung: eigenständiges, klar getrenntes Modul
  `src/mietinkasso/` im selben Repo/Branch (kein Import in beide
  Richtungen zu `invoice_automation`), damit es bei Bedarf portabel in
  ein eigenes Repo/Service verschoben werden kann.

## Implementiert

| Baustein | Ort | Status |
|---|---|---|
| Domäne (Enums, Money/Rundung inkl. Halbcent) | `domain/` | fertig + getestet |
| Stammdaten (Gesellschaft/Objekt/Einheit/Vertrag/Komponenten/Kaution/Sperre) | `stammdaten/` | fertig + getestet |
| Auth/Rollen (Gesellschafts-Scoping, DB-seitig durchgesetzt) | `auth/` | fertig + getestet |
| Audit-Log | `audit/` | fertig (Basisversion, append-only) |
| Debitoren-Ledger (Eröffnung, Buchen, Storno/Korrektur, Saldo) | `op/` | fertig + getestet |
| Eröffnungs-CSV-Import (Gesamtsaldo/Einzel-OP) | `op/eroeffnung_import.py` | fertig + getestet |
| Mietvorschreibung (Entwurf→Sollstellung→Zustellung→Export) | `vorschreibung/` | fertig + getestet |
| Bankabgleich (CAMT.053/CSV-Import, Zuordnung, Rücklastschrift) | `bank/` | fertig + getestet |
| Indexanpassung (versionierte Klausel, Halbcent-Rundung) | `index/` | fertig + getestet |
| Betriebskostenabrechnung | `bk/` | fertig + getestet |
| Mahnwesen (genau 2 Stufen, Outbox, Sperren, kein Doppelversand) | `mahnwesen/` | fertig + getestet |
| Jobs/Scheduler-Sicherheit (idempotente Läufe) | `jobs/` | fertig + getestet |
| API + Read-Only-Dashboard | `api/` | fertig (Smoke-getestet, siehe unten) |
| Synthetic-Data-Seed | `scripts/seed_synthetic_data.py` | fertig, idempotent geprüft |
| Importvorlagen | `importtemplates/` | fertig, gegen echte Testfälle geprüft |

## Testergebnis

```
python -m pytest tests/mietinkasso -q    # 88 passed (Stand Korrekturrunde 2)
python -m pytest -q                       # 193 passed (105 invoice_automation + 88 mietinkasso)
```

Die bestehende `invoice_automation`-Testsuite ist unverändert grün
geblieben. `api/app.py` hat keine automatisierten HTTP-Tests (kein
`httpx` als zusätzliche Abhängigkeit eingeführt), wurde aber manuell
gegen eine echte SQLite-DB smoke-getestet (`/health`, `/`, Routing).
Das ist in `OFFENE_PUNKTE.md` als Lücke vermerkt.

## Beim Bauen selbst gefundene und behobene Fehler (Beispiele)

Diese Punkte lohnen eine gezielte Gegenprüfung durch Codex, weil sie
zeigen, wo die Fachregeln leicht falsch zu implementieren sind:

- **Skalierungsfehler bei der BK-Umlage:** `to_cents()` erwartet einen
  Euro-Betrag und multipliziert intern mit 100; die erste Version von
  `bk/service.py` hat das versehentlich auf einen bereits in Cent
  vorliegenden Wert angewendet (Faktor-100-Fehler). Durch den Test
  `test_weg_ruecklage_erzeugt_keinen_automatischen_mieter_op` sofort
  aufgefallen und behoben.
- **"Fälliger unstrittiger Rest" schloss Zahlungen fälschlich aus:**
  `faelligkeit_bekannt` wurde ursprünglich für ALLE OP-Typen als
  Ausschlusskriterium behandelt; da Zahlungen typischerweise kein
  eigenes Fälligkeitsdatum haben, wurden sie komplett aus dem mahnbaren
  Betrag herausgefiltert, statt die Forderung zu mindern. Fachlich
  korrekt: Zahlungen/Gutschriften mindern den mahnbaren Betrag immer,
  nur Forderungen (Eröffnung/Soll/Rücklastschrift) brauchen eine
  bekannte, verstrichene Fälligkeit. Gefunden über
  `test_zahlung_zwischen_planung_und_versand_stoppt_versand`.
- **Wall-Clock statt fachlichem Datum:** mehrere Stellen (`sollstellen`,
  `storniere_und_korrigiere`, `ergebnisse_buchen`) haben intern
  `date.today()` für `buchungsdatum` verwendet, statt das vom Aufrufer
  übergebene fachliche `heute` durchzureichen. In Produktion meist
  unsichtbar (beides ist "heute"), aber ein Determinismus-/Testbarkeits-
  problem und potenziell ein Bug bei Nachbuchungen mit Rückdatum. Alle
  drei Stellen nehmen jetzt ein explizites `heute`-Argument.

## Wichtige Konstruktionsentscheidungen

- **Geld:** integer Cent überall im Ledger; `Decimal` nur für die
  Index-Rechenformel und die gesetzliche Halbcent-Rundung
  (`domain/money.py`), die ausdrücklich von normaler kaufmännischer
  Rundung getrennt implementiert ist.
- **Idempotenz:** jede importierte Zeile (OP, Bank) trägt eine
  `import_id` + `quelle_hash`. Wiederholung mit gleichem Inhalt ist ein
  No-Op; gleiche ID mit anderem Inhalt ist ein `ImportConflictError`.
- **Korrektur statt Überschreiben:** `op/service.py::storniere_und_korrigiere`
  markiert die Originalzeile `STORNIERT` und fügt bei Bedarf eine neue
  aktive Zeile hinzu; nichts wird in-place verändert.
- **Tenant-Trennung DB-seitig:** `bank/repository.py::create_zuordnung`
  vergleicht die Gesellschaft von Banktransaktion und Zielkonto anhand
  frisch aus der DB gelesener Zeilen und lehnt sonst mit
  `CrossTenantError` ab — nicht nur eine Service-Konvention.
- **Keine Heuristiken:** automatische Zahlungszuordnung nur über eine
  eindeutige, explizite Vertragsreferenz; Nutzungsstatus wird nie aus
  Soll=0 abgeleitet; Namens-/Betragsgleichheit allein reicht nirgends.
- **Mahnwesen auf Forderungsebene, nicht Vertragsebene:** jede
  Forderung (die sie erzeugende OP-Zeile) hat ihren eigenen
  Stufe1->Stufe2-Zyklus; Zahlungen werden den Forderungen FIFO
  (älteste Fälligkeit zuerst) zugeordnet, um den individuellen
  Reststand zu ermitteln (`op/service.py::offene_forderungen`).
- **Validieren vor Buchen:** jede OP-Buchung, die aus einer Bank-Zuordnung
  entsteht, wird erst validiert (Gesellschaft, Betrag, Währung,
  Restbetrag), dann gebucht — eine fehlgeschlagene Prüfung darf nie eine
  Ledger-Nebenwirkung hinterlassen.
