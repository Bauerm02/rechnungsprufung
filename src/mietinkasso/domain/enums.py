"""Domain enums for the Hausverwaltung & Mietinkasso module.

Kept deliberately explicit (no free-text status strings) so that every
state transition is enforceable in the service layer and reviewable by
a non-developer against the Fachregeln in docs/hausverwaltung/RAHMENPROGRAMM.md.
"""

from __future__ import annotations

import enum


class Nutzungsstatus(str, enum.Enum):
    DAUERVERMIETUNG = "DAUERVERMIETUNG"
    KURZZEITVERMIETUNG = "KURZZEITVERMIETUNG"
    EIGENNUTZUNG = "EIGENNUTZUNG"
    LEERSTAND = "LEERSTAND"
    SELFSTORAGE = "SELFSTORAGE"


class OPTyp(str, enum.Enum):
    EROEFFNUNG = "EROEFFNUNG"
    SOLL = "SOLL"
    GUTSCHRIFT = "GUTSCHRIFT"
    ZAHLUNG = "ZAHLUNG"
    RUECKLASTSCHRIFT = "RUECKLASTSCHRIFT"
    KORREKTUR = "KORREKTUR"


class OPPositionStatus(str, enum.Enum):
    AKTIV = "AKTIV"
    STORNIERT = "STORNIERT"


class VorschreibungStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    SOLLGESTELLT = "SOLLGESTELLT"
    ZUGESTELLT = "ZUGESTELLT"
    EXPORTIERT = "EXPORTIERT"


class MahnStufe(int, enum.Enum):
    STUFE_1 = 1
    STUFE_2 = 2


class MahnStatus(str, enum.Enum):
    GEPLANT = "GEPLANT"
    IN_VERSAND = "IN_VERSAND"  # atomar beansprucht, Provider-Aufruf läuft/lief möglicherweise
    GESENDET = "GESENDET"
    UNSICHER = "UNSICHER"
    BLOCKIERT = "BLOCKIERT"
    UEBERSPRUNGEN = "UEBERSPRUNGEN"


class Sperrgrund(str, enum.Enum):
    STREIT = "STREIT"
    MIETMINDERUNG = "MIETMINDERUNG"
    RATENPLAN = "RATENPLAN"
    INSOLVENZ = "INSOLVENZ"
    RECHTSANWALT = "RECHTSANWALT"
    UNGEKLAERTER_EINGANG = "UNGEKLAERTER_EINGANG"
    UNKLARER_EROEFFNUNGSSALDO = "UNKLARER_EROEFFNUNGSSALDO"
    BOUNCE = "BOUNCE"
    MANUELL = "MANUELL"


class BKPositionsart(str, enum.Enum):
    UMLAGEFAEHIG = "UMLAGEFAEHIG"
    EIGENTUEMER = "EIGENTUEMER"
    UNGEKLAERT = "UNGEKLAERT"


class BKAbrechnungStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    GEPRUEFT = "GEPRUEFT"
    FREIGEGEBEN = "FREIGEGEBEN"


class IndexKlauselStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    FREIGEGEBEN = "FREIGEGEBEN"
    GESPERRT = "GESPERRT"


class IndexAnpassungStatus(str, enum.Enum):
    VORSCHLAG = "VORSCHLAG"
    FREIGEGEBEN = "FREIGEGEBEN"
    VERWORFEN = "VERWORFEN"
    INVALIDIERT = "INVALIDIERT"


class Rechtsordnung(str, enum.Enum):
    OESTERREICH_MRG_VOLL = "OESTERREICH_MRG_VOLL"
    OESTERREICH_MRG_TEIL = "OESTERREICH_MRG_TEIL"
    OESTERREICH_MRG_FREI = "OESTERREICH_MRG_FREI"
    OESTERREICH_WGG = "OESTERREICH_WGG"
    OESTERREICH_GEWERBE = "OESTERREICH_GEWERBE"
    DEUTSCHLAND = "DEUTSCHLAND"
    # Bewusst kein Rateversuch: eine noch nicht geklärte rechtliche
    # Einordnung wird explizit als UNGEKLAERT erfasst statt eine der
    # anderen Kategorien zu raten. Ein Vertrag mit dieser Rechtsordnung
    # bleibt stammdatenseitig anlegbar, ist aber technisch von
    # Mahnung, Index-Anpassung und Sollstellung gesperrt (siehe
    # `stammdaten/repository.py`, `mahnwesen/service.py`,
    # `index/service.py`, `vorschreibung/service.py`).
    UNGEKLAERT = "UNGEKLAERT"


def rechtsordnung_geklaert(rechtsordnung: str) -> bool:
    """Zentrale Prüfung, die von `vorschreibung/service.py::sollstellen`,
    `index/service.py::klausel_anlegen`/`berechne_vorschlag` und
    `mahnwesen/service.py::plane_forderung` gleichermaßen benutzt wird,
    damit die drei Sperren nicht unabhängig voneinander (und potenziell
    inkonsistent) je Aufrufer neu formuliert werden."""

    return rechtsordnung != Rechtsordnung.UNGEKLAERT.value


class Rolle(str, enum.Enum):
    ADMIN = "ADMIN"
    BUCHHALTUNG = "BUCHHALTUNG"
    HAUSVERWALTUNG = "HAUSVERWALTUNG"
    LESEZUGRIFF = "LESEZUGRIFF"


class ZahlungsMatchTyp(str, enum.Enum):
    AUTOMATISCH_EINDEUTIG = "AUTOMATISCH_EINDEUTIG"
    MANUELL = "MANUELL"


class JobLaufStatus(str, enum.Enum):
    LAEUFT = "LAEUFT"
    ERFOLGREICH = "ERFOLGREICH"
    FEHLGESCHLAGEN = "FEHLGESCHLAGEN"
