from __future__ import annotations


class MietinkassoError(Exception):
    """Base class for all domain errors in this module."""


class ImportConflictError(MietinkassoError):
    """Same import_id was resubmitted with different content (a real change,
    not a replay). Refuses the write; caller must resolve manually."""


class CrossTenantError(MietinkassoError):
    """An operation attempted to read or link data across Gesellschaften."""


class SperreAktivError(MietinkassoError):
    """A Vertrag has an active Sperre blocking the requested action."""


class DoppelteEroeffnungsartError(MietinkassoError):
    """A Konto was opened with Einzel-OP and Gesamtsaldo at once, which
    would double-book the same old journal."""


class UnbekannteFaelligkeitError(MietinkassoError):
    """Raised only where code would otherwise silently assume a due date."""


class IndexKlauselFehltError(MietinkassoError):
    """No freigegebene IndexKlausel exists; an increase must not proceed."""


class VorschreibungBereitsVorhandenError(MietinkassoError):
    """Vertrag/Monat has already been vorgeschrieben (unique constraint)."""


class MahnstufeReihenfolgeError(MietinkassoError):
    """Stage 2 was requested without a successfully sent Stage 1."""


class BankstandVeraltetError(MietinkassoError):
    """The bank data used for a Mahnlauf is older than the configured max age."""


class MehrfachbuchungsKonfliktError(MietinkassoError):
    """A bank row without a stable native transaction ID matches the natural
    key (Konto/Datum/Betrag/Referenz) of an already-imported row. This could
    be the same transaction re-appearing in an overlapping export, or two
    genuinely different payments that happen to look identical - which one
    it is can't be decided automatically, so the import is refused rather
    than silently merged or silently duplicated."""


class FremdwaehrungNichtUnterstuetztError(MietinkassoError):
    """A bank row or manual allocation uses a currency other than the
    Konto's ledger currency. MVP1 has no FX handling, so this blocks rather
    than silently treating the amount as if it were EUR."""


class ZuordnungUngueltigError(MietinkassoError):
    """A requested Zuordnung/Zahlungszuordnung fails validation (amount,
    remaining balance, Konto/Vertrag mismatch, wrong OP type, ...)."""


class VorgangIdKonfliktError(MietinkassoError):
    """A `vorgang_id` was reused for a different bank_transaktion_id/
    op_position_id/betrag_cent than the Zuordnung already stored under it.
    A `vorgang_id` may only ever be replayed for the IDENTICAL operation
    (safe retry); reuse for a different operation is a caller bug, not an
    idempotent no-op, and must not silently return the old Zuordnung while
    a newly booked OP position is left standing."""


class RechtsprofilNichtImplementiertError(MietinkassoError):
    """An IndexKlausel references a Rechtsordnung/Berechnungsprofil this
    codebase does not actually implement (e.g. April-Termine,
    Jahresdurchschnittsbildung, anteilige Erstvalorisierung,
    Altvertragsübergang). Blocks the calculation instead of silently
    applying the generic threshold/damping formula and overclaiming legal
    correctness."""


class NachweisFehltError(MietinkassoError):
    """A status transition that claims a real-world effect (Zustellung,
    Hauptbuch-Export) was requested without the adapter confirmation/proof
    that would make that claim true."""


class BindungInkonsistentError(MietinkassoError):
    """A Konto/Vertrag/Gesellschaft/MahnFall combination passed to a service
    does not actually match what the database holds; refuses to act on
    caller-supplied objects that could be stale or mismatched."""


class UStSatzUngueltigError(MietinkassoError):
    """A VertragsKomponente/VorschreibungPosition carries a
    ust_satz_promille outside the fixed set of plausible Austrian
    Vermietung rates (0/10/20%). Blocks the Vorschreibungsentwurf instead
    of silently computing Netto/USt from an implausible source value."""


class StornierungKonfliktError(MietinkassoError):
    """`storniere_und_korrigiere`/`OPRepository.storno` wurde ein zweites
    Mal für ein bereits STORNIERTES Original aufgerufen. Nur ein exakt
    identischer Retry (gleiche `vorgang_id`, gleicher Inhalt - z. B. eine
    doppelte Formularbestätigung) ist ein sicherer No-Op und liefert die
    bereits angelegte Ersatzzeile zurück; jede Abweichung (anderer
    Betrag/Grund/keine vorgang_id) wird als Konflikt abgelehnt, statt eine
    ZWEITE aktive Ersatzzeile für dasselbe Original anzulegen."""


class IntakeNichtAnwendbarError(MietinkassoError):
    """`intake/apply.py::wende_an` wurde mit einem Paket aufgerufen, das
    bei der (unmittelbar vor dem Schreiben erneut durchgeführten)
    Prüfung Konflikte oder gesperrte Zeilen enthält (unbekannte
    Referenz, Objekt 107, abweichender Inhalt derselben ID, oder eine
    Eröffnungszeile ohne `quelle_bestaetigt: true`). Nichts wird
    geschrieben - "Ein Fehler => gesamter Lauf unverändert"."""


class RechtsordnungUngeklaertError(MietinkassoError):
    """Ein Vertrag trägt (noch) die Rechtsordnung UNGEKLAERT. Blockiert
    Sollstellung/Index-Anpassung explizit, statt eine der anderen
    Kategorien zu raten - Mahnung wird über dieselbe Prüfung als
    BLOCKIERT ausgewiesen (siehe `mahnwesen/service.py::plane_forderung`,
    das keine Exception wirft, sondern ein PlanungsErgebnis liefert)."""


class ObjektAusgeschlossenError(MietinkassoError):
    """Objekt 107 (Sieben Dörfer) is explicitly out of scope for the pilot.
    Enforced centrally (Konto -> Vertrag -> Einheit -> Objekt) on every
    writing financial path (Eröffnung, Nachbuchung/Zahlungszuordnung,
    Vorschreibung, BK, Index, Mahnwesen), not just as an isolated helper a
    caller could forget to invoke - and unconditionally for every role,
    ADMIN included."""
