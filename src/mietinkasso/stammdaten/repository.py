from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import ObjektAusgeschlossenError
from mietinkasso.infrastructure.db.tables import (
    DebitorTable,
    EinheitTable,
    GesellschaftTable,
    KautionTable,
    KontoTable,
    MietvertragsprofilTable,
    ObjektTable,
    SperreTable,
    VertragTable,
    VertragsKomponenteTable,
)


class StammdatenRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Gesellschaft ---------------------------------------------------
    def upsert_gesellschaft(self, *, id: str, name: str, session: Session | None = None) -> None:
        """`session`: siehe `set_eroeffnung_modus` - übergeben, um diese
        Schreibung Teil einer größeren, vom Aufrufer verwalteten
        Mehr-Entitäten-Transaktion zu machen (z. B. der generische
        Echtbetrieb-Intake in `intake/apply.py`, der Gesellschaft, Objekt,
        Einheit, Debitor, Vertrag und Eröffnung/Nachbuchungen einer
        gesamten Datei atomar in EINER Session verbucht)."""

        if session is not None:
            row = session.get(GesellschaftTable, id)
            if row is None:
                session.add(GesellschaftTable(id=id, name=name))
            else:
                row.name = name
            session.flush()
            return
        with self._session_factory() as owned_session:
            row = owned_session.get(GesellschaftTable, id)
            if row is None:
                owned_session.add(GesellschaftTable(id=id, name=name))
            else:
                row.name = name
            owned_session.commit()

    def get_gesellschaft(self, id: str) -> GesellschaftTable | None:
        with self._session_factory() as session:
            return session.get(GesellschaftTable, id)

    def list_gesellschaften(self) -> list[GesellschaftTable]:
        with self._session_factory() as session:
            return list(session.execute(select(GesellschaftTable).order_by(GesellschaftTable.id)).scalars().all())

    # -- Objekt -----------------------------------------------------------
    def upsert_objekt(
        self,
        *,
        id: str,
        gesellschaft_id: str,
        bezeichnung: str,
        adresse: str | None = None,
        ausgeschlossen: bool = False,
        session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft`."""

        def _schreiben(active_session: Session) -> None:
            row = active_session.get(ObjektTable, id)
            if row is None:
                active_session.add(
                    ObjektTable(
                        id=id,
                        gesellschaft_id=gesellschaft_id,
                        bezeichnung=bezeichnung,
                        adresse=adresse,
                        ausgeschlossen=ausgeschlossen,
                    )
                )
            else:
                row.gesellschaft_id = gesellschaft_id
                row.bezeichnung = bezeichnung
                row.adresse = adresse
                row.ausgeschlossen = ausgeschlossen

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_objekt(self, id: str) -> ObjektTable | None:
        with self._session_factory() as session:
            return session.get(ObjektTable, id)

    def list_objekte(self, *, gesellschaft_id: str | None = None) -> list[ObjektTable]:
        with self._session_factory() as session:
            statement = select(ObjektTable).order_by(ObjektTable.id)
            if gesellschaft_id is not None:
                statement = statement.where(ObjektTable.gesellschaft_id == gesellschaft_id)
            return list(session.execute(statement).scalars().all())

    # -- Einheit ----------------------------------------------------------
    def upsert_einheit(
        self,
        *,
        id: str,
        objekt_id: str,
        bezeichnung: str,
        nutzungsstatus: str,
        flaeche_qm: Decimal | None = None,
        miteigentumsanteile: Decimal | None = None,
        session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft`. Funktioniert bewusst
        auch ohne zugehörigen Vertrag - eine Einheit mit Nutzungsstatus
        LEERSTAND/SELFSTORAGE/KURZZEITVERMIETUNG ist ohne aktive
        Mietforderung erfassbar (keine Namens-/Nullsaldo-Heuristik)."""

        def _schreiben(active_session: Session) -> None:
            row = active_session.get(EinheitTable, id)
            if row is None:
                active_session.add(
                    EinheitTable(
                        id=id,
                        objekt_id=objekt_id,
                        bezeichnung=bezeichnung,
                        nutzungsstatus=nutzungsstatus,
                        flaeche_qm=flaeche_qm,
                        miteigentumsanteile=miteigentumsanteile,
                    )
                )
            else:
                row.objekt_id = objekt_id
                row.bezeichnung = bezeichnung
                row.nutzungsstatus = nutzungsstatus
                row.flaeche_qm = flaeche_qm
                row.miteigentumsanteile = miteigentumsanteile

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_einheit(self, id: str) -> EinheitTable | None:
        with self._session_factory() as session:
            return session.get(EinheitTable, id)

    def list_einheiten_fuer_objekt(self, objekt_id: str) -> list[EinheitTable]:
        with self._session_factory() as session:
            statement = select(EinheitTable).where(EinheitTable.objekt_id == objekt_id).order_by(EinheitTable.id)
            return list(session.execute(statement).scalars().all())

    def set_nutzungsstatus(self, *, einheit_id: str, nutzungsstatus: str) -> None:
        """Explicit, human-triggered status change. Never called by ledger
        code just because Soll==0 for a period (Leerstand must not be
        inferred automatically)."""

        with self._session_factory() as session:
            row = session.get(EinheitTable, einheit_id)
            if row is None:
                raise ValueError(f"Unbekannte Einheit {einheit_id}")
            row.nutzungsstatus = nutzungsstatus
            session.commit()

    # -- Debitor ------------------------------------------------------------
    def upsert_debitor(
        self, *, id: str, name: str, email: str | None = None, adresse: str | None = None,
        postadresse_geprueft: bool | None = None, session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft`. `postadresse_geprueft`:
        NUR bei explizitem Setzen (`True`/`False`) verändert - `None`
        (Default) lässt ein bereits gesetztes Flag unverändert, damit ein
        routinemäßiges Update (z. B. eine korrigierte E-Mail) eine einmal
        erteilte Prüfung nicht stillschweigend zurücksetzt (siehe
        `DebitorTable.postadresse_geprueft`-Docstring)."""

        def _schreiben(active_session: Session) -> None:
            row = active_session.get(DebitorTable, id)
            if row is None:
                active_session.add(DebitorTable(
                    id=id, name=name, email=email, adresse=adresse,
                    postadresse_geprueft=bool(postadresse_geprueft) if postadresse_geprueft is not None else False,
                ))
            else:
                row.name = name
                row.email = email
                row.adresse = adresse
                if postadresse_geprueft is not None:
                    row.postadresse_geprueft = postadresse_geprueft

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_debitor(self, id: str) -> DebitorTable | None:
        with self._session_factory() as session:
            return session.get(DebitorTable, id)

    def list_alle_debitoren(self) -> list[DebitorTable]:
        with self._session_factory() as session:
            return list(session.execute(select(DebitorTable).order_by(DebitorTable.id)).scalars().all())

    # -- Vertrag ------------------------------------------------------------
    def upsert_vertrag(
        self,
        *,
        id: str,
        einheit_id: str,
        debitor_id: str,
        gesellschaft_id: str,
        rechtsordnung: str,
        gueltig_von: date,
        gueltig_bis: date | None = None,
        faelligkeit_tag: int = 5,
        zahlungsfrist_tage: int = 14,
        session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft`."""

        def _schreiben(active_session: Session) -> None:
            row = active_session.get(VertragTable, id)
            if row is None:
                active_session.add(
                    VertragTable(
                        id=id,
                        einheit_id=einheit_id,
                        debitor_id=debitor_id,
                        gesellschaft_id=gesellschaft_id,
                        rechtsordnung=rechtsordnung,
                        gueltig_von=gueltig_von,
                        gueltig_bis=gueltig_bis,
                        faelligkeit_tag=faelligkeit_tag,
                        zahlungsfrist_tage=zahlungsfrist_tage,
                    )
                )
            else:
                row.einheit_id = einheit_id
                row.debitor_id = debitor_id
                row.gesellschaft_id = gesellschaft_id
                row.rechtsordnung = rechtsordnung
                row.gueltig_von = gueltig_von
                row.gueltig_bis = gueltig_bis
                row.faelligkeit_tag = faelligkeit_tag
                row.zahlungsfrist_tage = zahlungsfrist_tage

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_vertrag(self, id: str) -> VertragTable | None:
        with self._session_factory() as session:
            return session.get(VertragTable, id)

    def list_alle_vertraege(self) -> list[VertragTable]:
        """Für Batch-/Automatikläufe über ALLE Verträge (Indexautomatik,
        Vertragsende-Erinnerung) - anders als `list_vertraege_fuer_objekt`
        ohne Objektfilter. Schließt Objekt 107 NICHT selbst aus (das bleibt
        `pruefe_vertrag_nicht_ausgeschlossen`, das der jeweilige Aufrufer
        pro Vertrag aufruft), damit ein ausgeschlossener Vertrag sichtbar
        mit Grund übersprungen wird statt lautlos zu fehlen."""

        with self._session_factory() as session:
            return list(session.execute(select(VertragTable).order_by(VertragTable.id)).scalars().all())

    def list_vertraege_fuer_objekt(self, objekt_id: str) -> list[VertragTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragTable)
                .join(EinheitTable, EinheitTable.id == VertragTable.einheit_id)
                .where(EinheitTable.objekt_id == objekt_id)
                .order_by(VertragTable.id)
            )
            return list(session.execute(statement).scalars().all())

    def objekt_fuer_vertrag(self, vertrag_id: str, *, session: Session | None = None) -> ObjektTable:
        """Mit einer übergebenen `session` wird ausschließlich SIE für die
        Lesungen verwendet, statt eine eigene, separate Session zu öffnen.
        Wichtig innerhalb einer größeren atomaren Mehrzeilen-Transaktion
        (z. B. `op/eroeffnung_import.py::importiere_eroeffnung_csv_atomar`):
        bei SQLite `:memory:`/`StaticPool` teilen sich mehrere Sessions
        dieselbe physische Verbindung - eine separat geöffnete,
        schreibfreie Hilfs-Session würde beim Schließen (impliziter
        Rollback) die bereits geflushten, aber noch nicht committeten
        Änderungen der ÄUSSEREN Transaktion mit zurückrollen. Deshalb hier
        konsequent dieselbe Session weiterreichen, statt eine neue zu
        öffnen."""

        def _query(active_session: Session) -> ObjektTable:
            vertrag = active_session.get(VertragTable, vertrag_id)
            if vertrag is None:
                raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
            einheit = active_session.get(EinheitTable, vertrag.einheit_id)
            if einheit is None:
                raise ValueError(f"Unbekannte Einheit {vertrag.einheit_id}")
            objekt = active_session.get(ObjektTable, einheit.objekt_id)
            if objekt is None:
                raise ValueError(f"Unbekanntes Objekt {einheit.objekt_id}")
            return objekt

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def pruefe_vertrag_nicht_ausgeschlossen(self, vertrag_id: str, *, session: Session | None = None) -> None:
        """Zentrale, unumgängliche Durchsetzung von Fachregel 1 (Objekt 107
        ausgeschlossen): löst Konto/Vertrag -> Einheit -> Objekt frisch aus
        der DB auf und blockiert JEDEN schreibenden Finanzpfad, unabhängig
        von Rolle (auch ADMIN) - nicht nur eine isolierte Hilfsfunktion, die
        ein Aufrufer vergessen könnte zu rufen. `session`: siehe
        `objekt_fuer_vertrag`."""

        objekt = self.objekt_fuer_vertrag(vertrag_id, session=session)
        if objekt.ausgeschlossen:
            raise ObjektAusgeschlossenError(
                f"Vertrag {vertrag_id} gehört zu Objekt {objekt.id}, das von der Pilotphase "
                "ausgeschlossen ist (z. B. 107 Sieben Dörfer)."
            )

    def pruefe_konto_nicht_ausgeschlossen(self, konto: KontoTable, *, session: Session | None = None) -> None:
        self.pruefe_vertrag_nicht_ausgeschlossen(konto.vertrag_id, session=session)

    def objekt_fuer_einheit(self, einheit_id: str, *, session: Session | None = None) -> ObjektTable:
        """Wie `objekt_fuer_vertrag`, aber direkt über die Einheit - für
        Fälle OHNE Vertrag (z. B. KURZZEITVERMIETUNG/SELFSTORAGE-
        Monatsabrechnungen, `variableabrechnung/`), die keinen
        Dauervermietungs-Vertrag voraussetzen."""

        def _query(active_session: Session) -> ObjektTable:
            einheit = active_session.get(EinheitTable, einheit_id)
            if einheit is None:
                raise ValueError(f"Unbekannte Einheit {einheit_id}")
            objekt = active_session.get(ObjektTable, einheit.objekt_id)
            if objekt is None:
                raise ValueError(f"Unbekanntes Objekt {einheit.objekt_id}")
            return objekt

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def pruefe_einheit_nicht_ausgeschlossen(self, einheit_id: str, *, session: Session | None = None) -> None:
        """Wie `pruefe_vertrag_nicht_ausgeschlossen`, aber für einen
        Schreibpfad, der nur eine Einheit (keinen Vertrag) referenziert."""

        objekt = self.objekt_fuer_einheit(einheit_id, session=session)
        if objekt.ausgeschlossen:
            raise ObjektAusgeschlossenError(
                f"Einheit {einheit_id} gehört zu Objekt {objekt.id}, das von der Pilotphase "
                "ausgeschlossen ist (z. B. 107 Sieben Dörfer)."
            )

    def add_komponente(
        self,
        *,
        id: str,
        vertrag_id: str,
        art: str,
        bezeichnung: str,
        betrag_cent: int,
        ust_satz_promille: int = 10000,
        indexierbar: bool = False,
        gueltig_von: date,
        gueltig_bis: date | None = None,
        historisiert_von_id: str | None = None,
        session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft`. Bewusst reines Insert
        (keine Upsert-Semantik wie bei den Stammdaten-Entitäten oben) - der
        generische Intake (`intake/apply.py`) prüft VOR dem Aufruf über
        `intake/planner.py`, ob dieselbe Komponenten-ID bereits identisch
        existiert (dann kein Aufruf, No-Op) oder mit abweichendem Inhalt
        (dann KONFLIKT, gesamter Lauf gesperrt) - ein zweiter Aufruf mit
        derselben ID landet hier also nie, außer bei einem Programmierfehler
        außerhalb des Intake-Pfads, wo ein `IntegrityError` bewusst laut
        scheitern soll statt eine bestehende Komponente stillschweigend zu
        überschreiben (keine Indexfreigabe/-berechnung ist hiervon berührt -
        `indexierbar` ist nur ein Eignungsflag für `index/service.py`, kein
        eigenständiger Freigabeschritt)."""

        def _schreiben(active_session: Session) -> None:
            active_session.add(
                VertragsKomponenteTable(
                    id=id,
                    vertrag_id=vertrag_id,
                    art=art,
                    bezeichnung=bezeichnung,
                    betrag_cent=betrag_cent,
                    ust_satz_promille=ust_satz_promille,
                    indexierbar=indexierbar,
                    gueltig_von=gueltig_von,
                    gueltig_bis=gueltig_bis,
                    historisiert_von_id=historisiert_von_id,
                )
            )

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_komponente(self, id: str, *, session: Session | None = None) -> VertragsKomponenteTable | None:
        """`session`: siehe `objekt_fuer_vertrag` - Pflicht, wenn die
        gesuchte Komponente innerhalb einer noch NICHT committeten
        äußeren Transaktion erst neu eingefügt wurde (z. B.
        `indexautomatik/umsetzung_service.py::umsetzen`); eine separat
        geöffnete Session sähe eine solche Zeile noch nicht."""

        if session is not None:
            return session.get(VertragsKomponenteTable, id)
        with self._session_factory() as owned_session:
            return owned_session.get(VertragsKomponenteTable, id)

    def ursprungs_gueltig_von(self, komponente: VertragsKomponenteTable, *, session: Session | None = None) -> date:
        """Verfolgt die explizite Historisierungs-Kette
        (`VertragsKomponenteTable.historisiert_von_id`, ausschließlich von
        `indexautomatik/umsetzung_service.py::umsetzen` gesetzt) bis zur
        URSPRÜNGLICHEN Zeile zurück und liefert deren `gueltig_von`.

        Eine Index-Umsetzung schließt die alte Komponentenzeile
        (`gueltig_bis`) und legt eine NEUE mit neuer ID/neuem `gueltig_von`
        (dem Anspruchsmonat) an - dieselbe, ununterbrochen fortbestehende
        vertragliche Verpflichtung, nur eine neue DB-Zeile (append-only
        Historisierung). Eine reine `komponente.gueltig_von`-Prüfung würde
        deshalb nach JEDER Umsetzung fälschlich behaupten, die Komponente
        habe zu einem davor liegenden, aber vollkommen legitimen
        historischen Bezugszeitpunkt (z. B. dem bewusst fortgeschriebenen
        MieWeG-`bezugsjahr`/`bezugsmonat`, siehe `umsetzung_service.py`-
        Docstring zum "Leerschritt") noch nicht bestanden.
        `historisiert_von_id` ist eine explizite, ausschließlich von
        unserem eigenen Code gesetzte Fremdschlüsselreferenz - KEINE
        ID-String-/Namens-Heuristik (AGENTS.md). Gemeinsam genutzt von
        `mieweg_vorschau/service.py` und `indexautomatik/rechtsprofil.py`,
        damit beide Existenzprüfungen konsistent bleiben."""

        aktuelle = komponente
        besucht = {aktuelle.id}
        while aktuelle.historisiert_von_id is not None:
            vorgaenger = self.get_komponente(aktuelle.historisiert_von_id, session=session)
            if vorgaenger is None or vorgaenger.id in besucht:
                break
            besucht.add(vorgaenger.id)
            aktuelle = vorgaenger
        return aktuelle.gueltig_von

    def list_aktive_komponenten(
        self, vertrag_id: str, stichtag: date, *, session: Session | None = None
    ) -> list[VertragsKomponenteTable]:
        """`session`: siehe `get_komponente` - Pflicht, wenn eine soeben
        (innerhalb derselben, noch NICHT committeten Transaktion) neu
        historisierte Komponente bereits berücksichtigt werden muss
        (`indexautomatik/umsetzung_service.py::umsetzen`, Aktualisierung
        noch offener Monatsvorschreibungs-Entwürfe)."""

        def _lesen(active_session: Session) -> list[VertragsKomponenteTable]:
            statement = (
                select(VertragsKomponenteTable)
                .where(VertragsKomponenteTable.vertrag_id == vertrag_id)
                .where(VertragsKomponenteTable.gueltig_von <= stichtag)
                .where(
                    (VertragsKomponenteTable.gueltig_bis.is_(None))
                    | (VertragsKomponenteTable.gueltig_bis >= stichtag)
                )
            )
            return list(active_session.execute(statement).scalars().all())

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def list_komponenten_im_zeitraum(self, vertrag_id: str, von: date, bis: date) -> list[VertragsKomponenteTable]:
        """Anders als `list_aktive_komponenten` (EIN Stichtag) liefert
        diese Methode JEDE Komponente, die den Zeitraum [von, bis]
        IRGENDWIE ÜBERLAPPT - auch eine Komponente, die erst MITTEN im
        Zeitraum beginnt oder mittendrin endet. Unabhängiger Review: eine
        Monatsübersicht, die nur zum Monatsersten aktive Komponenten
        prüft, macht eine unter dem Monat neu hinzugekommene/geänderte
        Komponente (z. B. Küchenmiete ab 15.08.) unsichtbar, statt sie
        als Datenlücke zu melden."""

        with self._session_factory() as session:
            statement = (
                select(VertragsKomponenteTable)
                .where(VertragsKomponenteTable.vertrag_id == vertrag_id)
                .where(VertragsKomponenteTable.gueltig_von <= bis)
                .where(
                    (VertragsKomponenteTable.gueltig_bis.is_(None))
                    | (VertragsKomponenteTable.gueltig_bis >= von)
                )
            )
            return list(session.execute(statement).scalars().all())

    # -- Kaution (strictly separate from OP) ---------------------------------
    def set_kaution(
        self, *, id: str, vertrag_id: str, betrag_cent: int, stichtag: date, referenz: str | None = None,
        session: Session | None = None,
    ) -> None:
        """`session`: siehe `upsert_gesellschaft` - erlaubt dem generischen
        Intake (`intake/apply.py`), eine Kaution Teil derselben atomaren
        Mehr-Entitäten-Transaktion zu machen."""

        def _schreiben(active_session: Session) -> None:
            existing = active_session.execute(
                select(KautionTable).where(KautionTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            if existing is None:
                active_session.add(
                    KautionTable(
                        id=id, vertrag_id=vertrag_id, betrag_cent=betrag_cent, stichtag=stichtag, referenz=referenz
                    )
                )
            else:
                existing.betrag_cent = betrag_cent
                existing.stichtag = stichtag
                existing.referenz = referenz

        if session is not None:
            _schreiben(session)
            session.flush()
            return
        with self._session_factory() as owned_session:
            _schreiben(owned_session)
            owned_session.commit()

    def get_kaution(self, vertrag_id: str, *, session: Session | None = None) -> KautionTable | None:
        if session is not None:
            return session.execute(select(KautionTable).where(KautionTable.vertrag_id == vertrag_id)).scalar_one_or_none()
        with self._session_factory() as owned_session:
            return owned_session.execute(select(KautionTable).where(KautionTable.vertrag_id == vertrag_id)).scalar_one_or_none()

    # -- Mietvertragsprofil (Auftrag HV-20260913-VERTRAGSANLAGE) ---------------
    def add_mietvertragsprofil(
        self,
        *,
        vertrag_id: str,
        nutzungsart: str,
        quelle_typ: str,
        erstellt_von: str,
        urspruenglicher_mietbeginn: date | None = None,
        verwaltungsuebernahme_am: date | None = None,
        verwaltung_bezeichnung: str | None = None,
        vertragliche_kaution_cent: int | None = None,
        vertragliche_kaution_quellenbeleg: str | None = None,
        mahngebuehr_cent: int | None = None,
        mahngebuehr_quellenbeleg: str | None = None,
        index_reihe: str | None = None,
        index_urspruenglicher_basismonat: str | None = None,
        index_urspruenglicher_basiswert: Decimal | None = None,
        index_schwelle_prozent: Decimal | None = None,
        index_schwelle_inklusive: bool | None = None,
        index_anpassungsmonat: int | None = None,
        index_mindestintervall_monate: int | None = None,
        index_klauseltext_auszug: str | None = None,
        index_klauseltext_seite: int | None = None,
        quelle_referenz: str | None = None,
        session: Session | None = None,
    ) -> MietvertragsprofilTable:
        """Reines Insert einer NEUEN Version (append-only, siehe
        `MietvertragsprofilTable`-Docstring) - NIE ein In-Place-Update einer
        bestehenden Zeile. Die Versionsnummer wird hier aus der zuletzt
        gespeicherten Version fortgeschrieben (analog `add_komponente`s
        Historisierungs-Pattern); der Aufrufer (`intake/apply.py`) entscheidet
        über `intake/planner.py`, OB eine neue Version nötig ist (Status
        NEU/AKTUALISIERUNG) - ein Aufruf hier landet nur bei tatsächlich
        abweichendem Inhalt oder einer ersten Anlage."""

        def _schreiben(active_session: Session) -> MietvertragsprofilTable:
            bisherige_version = active_session.execute(
                select(MietvertragsprofilTable.version)
                .where(MietvertragsprofilTable.vertrag_id == vertrag_id)
                .order_by(MietvertragsprofilTable.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            row = MietvertragsprofilTable(
                vertrag_id=vertrag_id,
                version=(bisherige_version or 0) + 1,
                nutzungsart=nutzungsart,
                urspruenglicher_mietbeginn=urspruenglicher_mietbeginn,
                verwaltungsuebernahme_am=verwaltungsuebernahme_am,
                verwaltung_bezeichnung=verwaltung_bezeichnung,
                vertragliche_kaution_cent=vertragliche_kaution_cent,
                vertragliche_kaution_quellenbeleg=vertragliche_kaution_quellenbeleg,
                mahngebuehr_cent=mahngebuehr_cent,
                mahngebuehr_quellenbeleg=mahngebuehr_quellenbeleg,
                index_reihe=index_reihe,
                index_urspruenglicher_basismonat=index_urspruenglicher_basismonat,
                index_urspruenglicher_basiswert=index_urspruenglicher_basiswert,
                index_schwelle_prozent=index_schwelle_prozent,
                index_schwelle_inklusive=index_schwelle_inklusive,
                index_anpassungsmonat=index_anpassungsmonat,
                index_mindestintervall_monate=index_mindestintervall_monate,
                index_klauseltext_auszug=index_klauseltext_auszug,
                index_klauseltext_seite=index_klauseltext_seite,
                quelle_typ=quelle_typ,
                quelle_referenz=quelle_referenz,
                erstellt_von=erstellt_von,
            )
            active_session.add(row)
            return row

        if session is not None:
            row = _schreiben(session)
            session.flush()
            return row
        with self._session_factory() as owned_session:
            row = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(row)
            return row

    def neuestes_mietvertragsprofil(self, vertrag_id: str, *, session: Session | None = None) -> MietvertragsprofilTable | None:
        def _lesen(active_session: Session) -> MietvertragsprofilTable | None:
            return active_session.execute(
                select(MietvertragsprofilTable)
                .where(MietvertragsprofilTable.vertrag_id == vertrag_id)
                .order_by(MietvertragsprofilTable.version.desc())
                .limit(1)
            ).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def liste_mietvertragsprofil_versionen(self, vertrag_id: str) -> list[MietvertragsprofilTable]:
        """Für die Anzeige 'Quellen und Historie' im Backoffice-Detail -
        älteste zuerst."""
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(MietvertragsprofilTable)
                    .where(MietvertragsprofilTable.vertrag_id == vertrag_id)
                    .order_by(MietvertragsprofilTable.version.asc())
                ).scalars().all()
            )

    # -- Sperren --------------------------------------------------------------
    def sperre_setzen(
        self, *, vertrag_id: str, grund: str, kommentar: str | None = None, session: Session | None = None
    ) -> int:
        """`session`: siehe `upsert_gesellschaft` - erlaubt dem generischen
        Intake (`intake/apply.py`), eine Sperre Teil derselben atomaren
        Mehr-Entitäten-Transaktion zu machen. Bewusst reines Insert (eine
        Sperre ist ein additiver Fakt, kein Upsert-Ziel) - Wiederholimport-
        Sicherheit stellt der Aufrufer über `intake/planner.py` her (bereits
        aktive, inhaltsgleiche Sperre -> kein Aufruf)."""

        def _schreiben(active_session: Session) -> SperreTable:
            row = SperreTable(vertrag_id=vertrag_id, grund=grund, kommentar=kommentar)
            active_session.add(row)
            return row

        if session is not None:
            row = _schreiben(session)
            session.flush()
            return row.id
        with self._session_factory() as owned_session:
            row = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(row)
            return row.id

    def sperre_aufheben(self, sperre_id: int) -> None:
        from datetime import datetime, timezone

        with self._session_factory() as session:
            row = session.get(SperreTable, sperre_id)
            if row is None:
                raise ValueError(f"Unbekannte Sperre {sperre_id}")
            row.aufgehoben_am = datetime.now(timezone.utc)
            session.commit()

    def aktive_sperren(self, vertrag_id: str, *, session: Session | None = None) -> list[SperreTable]:
        """`session`: siehe `upsert_gesellschaft`."""

        def _lesen(active_session: Session) -> list[SperreTable]:
            statement = (
                select(SperreTable)
                .where(SperreTable.vertrag_id == vertrag_id)
                .where(SperreTable.aufgehoben_am.is_(None))
            )
            return list(active_session.execute(statement).scalars().all())

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def get_sperre(self, sperre_id: int) -> SperreTable | None:
        with self._session_factory() as session:
            return session.get(SperreTable, sperre_id)

    # -- Konto ------------------------------------------------------------
    def get_or_create_konto(self, *, vertrag: VertragTable, session: Session | None = None) -> KontoTable:
        """`session`: siehe `upsert_gesellschaft`."""

        def _lesen_oder_anlegen(active_session: Session) -> KontoTable:
            existing = active_session.execute(
                select(KontoTable).where(KontoTable.vertrag_id == vertrag.id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            konto = KontoTable(
                id=f"KTO-{vertrag.id}",
                vertrag_id=vertrag.id,
                debitor_id=vertrag.debitor_id,
                gesellschaft_id=vertrag.gesellschaft_id,
            )
            active_session.add(konto)
            active_session.flush()
            return konto

        if session is not None:
            return _lesen_oder_anlegen(session)
        with self._session_factory() as owned_session:
            konto = _lesen_oder_anlegen(owned_session)
            owned_session.commit()
            owned_session.refresh(konto)
            return konto

    def get_konto(self, konto_id: str) -> KontoTable | None:
        with self._session_factory() as session:
            return session.get(KontoTable, konto_id)

    def get_konto_by_vertrag(self, vertrag_id: str) -> KontoTable | None:
        with self._session_factory() as session:
            return session.execute(select(KontoTable).where(KontoTable.vertrag_id == vertrag_id)).scalar_one_or_none()

    def set_eroeffnung_modus(self, *, konto_id: str, modus: str, stichtag: date, session: Session | None = None) -> None:
        """Mit einer übergebenen `session` wird diese Schreibung Teil einer
        größeren, vom Aufrufer verwalteten Transaktion (z. B. der atomare
        Mehrzeilen-Eröffnungsimport in `op/eroeffnung_import.py`) - flush
        statt commit, damit ein späterer Fehler in derselben Datei auch
        diese Änderung mit zurückrollt."""

        if session is not None:
            row = session.get(KontoTable, konto_id)
            if row is None:
                raise ValueError(f"Unbekanntes Konto {konto_id}")
            row.eroeffnung_modus = modus
            row.eroeffnung_stichtag = stichtag
            session.flush()
            return
        with self._session_factory() as owned_session:
            row = owned_session.get(KontoTable, konto_id)
            if row is None:
                raise ValueError(f"Unbekanntes Konto {konto_id}")
            row.eroeffnung_modus = modus
            row.eroeffnung_stichtag = stichtag
            owned_session.commit()
