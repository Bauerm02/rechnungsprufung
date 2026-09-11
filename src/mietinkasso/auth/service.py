"""Server-side authorization: role + Gesellschaft (tenant) scoping.

Every repository call that touches Gesellschaft-scoped data must go
through `require_gesellschaft_access`. This is deliberately not
optional/best-effort: the Fachregeln require Gesellschaftstrennung
serverseitig, so scope checks live in the service/repository layer, not
only in the API/UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from mietinkasso.domain.enums import Rolle
from mietinkasso.domain.exceptions import CrossTenantError

_WRITE_ROLES = {Rolle.ADMIN, Rolle.BUCHHALTUNG, Rolle.HAUSVERWALTUNG}


@dataclass(frozen=True)
class AuthContext:
    """Represents the currently authenticated actor for audit + scoping."""

    user_id: str
    rolle: Rolle
    gesellschaft_ids: frozenset[str] | None  # None == alle Gesellschaften (ADMIN only)

    def has_zugriff(self, gesellschaft_id: str) -> bool:
        if self.gesellschaft_ids is None:
            return self.rolle == Rolle.ADMIN
        return gesellschaft_id in self.gesellschaft_ids

    def kann_schreiben(self) -> bool:
        return self.rolle in _WRITE_ROLES


def require_gesellschaft_access(ctx: AuthContext, gesellschaft_id: str) -> None:
    if not ctx.has_zugriff(gesellschaft_id):
        raise CrossTenantError(
            f"User {ctx.user_id} ({ctx.rolle.value}) hat keinen Zugriff auf Gesellschaft {gesellschaft_id}."
        )


def require_schreibrecht(ctx: AuthContext) -> None:
    if not ctx.kann_schreiben():
        raise CrossTenantError(f"User {ctx.user_id} ({ctx.rolle.value}) hat kein Schreibrecht.")
