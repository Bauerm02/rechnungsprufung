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
