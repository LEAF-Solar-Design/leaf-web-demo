"""Operator principal resolution (contract/OPERATOR.md section 1).

Read-only at the HTTP layer by construction: this module exposes lookup and
revalidation only. The single writer is the out-of-band CLI
(``scripts/operator_principal_admin.py``); no import of this module can
create, edit, or delete a principal. PostgreSQL errors propagate so callers
fail closed (503), never open.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _db():
    if "leaf_platform" not in sys.modules:
        package_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("platform package could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as db  # noqa: PLC0415
    return db


@dataclass(frozen=True)
class OperatorPrincipal:
    subject: str
    role: str
    role_revision: int
    status: str
    profiles: tuple
    environment: str

    @property
    def active(self) -> bool:
        return self.status == "active"


def resolve_principal(subject: str) -> Optional[OperatorPrincipal]:
    """Fresh read of the server-owned grant. None = no grant exists.

    Raises on database unavailability — the caller maps that to 503
    (fail closed), never to an implicit deny that hides an outage.
    """
    if not subject or not isinstance(subject, str):
        return None
    db = _db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT subject, role, role_revision, status, profiles,"
            " environment FROM operator_principals WHERE subject = %s",
            (subject,))
        row = cur.fetchone()
    if row is None:
        return None
    return OperatorPrincipal(
        subject=row["subject"], role=row["role"],
        role_revision=int(row["role_revision"]), status=row["status"],
        profiles=tuple(row["profiles"] or ()),
        environment=row["environment"])


def revalidate(subject: str, expected_role_revision: int) -> bool:
    """True only for an active principal whose role_revision still matches.

    Any drift — revocation, suspension, a role_revision bump, a vanished
    row — returns False and must deny the artifact being revalidated.
    """
    principal = resolve_principal(subject)
    if principal is None or not principal.active:
        return False
    return principal.role_revision == int(expected_role_revision)
