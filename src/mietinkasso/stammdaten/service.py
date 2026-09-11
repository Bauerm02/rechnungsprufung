from __future__ import annotations

from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.stammdaten.repository import StammdatenRepository


class ObjektAusgeschlossenError(MietinkassoError):
    """Objekt 107 (Sieben Dörfer) is explicitly out of scope for the pilot."""


class StammdatenService:
    def __init__(self, repository: StammdatenRepository):
        self._repository = repository

    def pruefe_objekt_erlaubt(self, objekt_id: str) -> None:
        objekt = self._repository.get_objekt(objekt_id)
        if objekt is not None and objekt.ausgeschlossen:
            raise ObjektAusgeschlossenError(
                f"Objekt {objekt_id} ist von der Pilotphase ausgeschlossen (107 Sieben Dörfer)."
            )

    def einheit_fuer_vertrag(self, vertrag_id: str):
        vertrag = self._repository.get_vertrag(vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
        return self._repository.get_einheit(vertrag.einheit_id)
