"""Internal, trusted Studio turn custody. This module never resolves authority.

Callers must freshly resolve canonical identity, project membership and the active
turn before constructing Authority. No request/model grant configuration belongs
here. Encrypted bytes alone cross the database boundary; no crypto runs in a tx.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import os
import re
import ipaddress
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID

PURPOSE = "studio-integration-turn-grant"
SCHEMA = 1
OPERATIONS = ("dataset_discovery", "dataset_query", "element_save")
MAX_ENVELOPE_BYTES = 32768


class GrantStoreError(Exception):
    """Fixed public error code, never a database/provider message."""


def _invalid():
    raise GrantStoreError("GRANT_INVALID") from None


def _uuid(value):
    if type(value) is not str:
        _invalid()
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        _invalid()
    if str(parsed) != value or parsed.int == 0:
        _invalid()


def _text(value, maximum):
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or value != value.strip()
            or any(unicodedata.category(c).startswith("C") for c in value)):
        _invalid()


def finite_time(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        _invalid()
    return value


@dataclass(frozen=True)
class TenantProject:
    tenant_id: str
    project_id: str

    def __post_init__(self):
        _uuid(self.tenant_id)
        _uuid(self.project_id)


@dataclass(frozen=True)
class Authority:
    tenant_id: str
    project_id: str
    platform_user_id: str
    binding_id: str
    external_authority: str
    subject: str
    session_id: str
    turn_id: str
    origin: str
    operations: tuple[str, ...] = OPERATIONS

    def __post_init__(self):
        for value in (self.tenant_id, self.project_id, self.platform_user_id, self.binding_id):
            _uuid(value)
        if self.external_authority != "auth0":
            _invalid()
        _text(self.subject, 512)
        _text(self.session_id, 200)
        _text(self.turn_id, 200)
        _text(self.origin, 2048)
        try:
            url = urlsplit(self.origin)
            port = url.port
            host = url.hostname or ""
            if ":" in host:
                if str(ipaddress.IPv6Address(host)) != host:
                    _invalid()
                canonical_host = "[" + host + "]"
            else:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
                    _invalid()
                if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                       for label in host.split(".")):
                    _invalid()
                canonical_host = host
            canonical_netloc = canonical_host + (":" + str(port) if port is not None else "")
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.password is not None or url.path or url.query or url.fragment
                    or url.netloc != url.netloc.lower() or port == 443
                    or self.origin != "https://" + url.netloc
                    or url.netloc != canonical_netloc
                    or "\\" in self.origin or "%" in self.origin
                    or url.hostname.endswith(".") or (port is not None and not 1 <= port <= 65535)):
                _invalid()
        except ValueError:
            _invalid()
        if type(self.operations) is not tuple or self.operations != OPERATIONS:
            _invalid()


@dataclass(frozen=True)
class Binding:
    authority: Authority
    grant_id: str
    issued_at: float
    verified_jwt_exp: float
    turn_deadline: float
    expires_at: float
    schema: int = SCHEMA
    purpose: str = PURPOSE

    def __post_init__(self):
        if type(self.authority) is not Authority:
            _invalid()
        self.authority.__post_init__()
        _uuid(self.grant_id)
        if UUID(self.grant_id).version != 4:
            _invalid()
        for value in (self.issued_at, self.verified_jwt_exp, self.turn_deadline, self.expires_at):
            finite_time(value)
        if (type(self.schema) is not int or self.schema != SCHEMA or self.purpose != PURPOSE
                or not self.issued_at < self.expires_at <= min(
                    self.verified_jwt_exp, self.turn_deadline, self.issued_at + 120)):
            _invalid()

    def to_dict(self):
        self.__post_init__()
        result = asdict(self)
        result.pop("authority")
        result.update(asdict(self.authority))
        result["operations"] = list(self.authority.operations)
        return result

    def canonical_bytes(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")

    @classmethod
    def from_dict(cls, value):
        try:
            expected = (set(Authority.__dataclass_fields__)
                        | (set(cls.__dataclass_fields__) - {"authority"}))
            if type(value) is not dict or set(value) != expected:
                _invalid()
            data = dict(value)
            fields = Authority.__dataclass_fields__
            auth = {name: data.pop(name) for name in fields}
            if type(auth["operations"]) is not list:
                _invalid()
            auth["operations"] = tuple(auth["operations"])
            return cls(authority=Authority(**auth), **data)
        except Exception:
            raise GrantStoreError("GRANT_INVALID") from None


@dataclass(frozen=True)
class EncryptedGrant:
    binding: Binding
    envelope: bytes


def envelope_bytes(envelope):
    if type(envelope) is not bytes or not 1 <= len(envelope) <= MAX_ENVELOPE_BYTES:
        _invalid()
    # Shared strict parser: validation happens before connecting to PostgreSQL.
    try:
        try:
            from server.integration_turn_grant_crypto import validate_envelope
        except ModuleNotFoundError as exc:
            if exc.name != "server":
                raise
            from integration_turn_grant_crypto import validate_envelope
        validate_envelope(envelope)
    except Exception:
        raise GrantStoreError("GRANT_INVALID") from None
    return envelope


def _default_connection():
    # Do not allow the shared helper to load .env.local for this custody path.
    if not os.environ.get("DATABASE_URL"):
        raise GrantStoreError("GRANT_UNAVAILABLE")
    try:
        from server.platform_link import platform_db
    except ModuleNotFoundError as exc:
        if exc.name != "server":
            raise
        from platform_link import platform_db
    return platform_db().connection()


_GUARD = """
SELECT (NOT r.rolsuper AND NOT r.rolbypassrls
        AND c.relowner <> r.oid AND NOT pg_has_role(current_user, c.relowner, 'MEMBER')
        AND c.relrowsecurity AND c.relforcerowsecurity
        AND EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid
                    AND p.polname='integration_turn_grant_scope' AND p.polpermissive
                    AND p.polqual IS NOT NULL AND p.polwithcheck IS NOT NULL)
        AND (SELECT count(*) FROM pg_policy p WHERE p.polrelid=c.oid)=1) AS safe
FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE r.rolname=current_user AND n.nspname='public' AND c.relname='integration_turn_grants'
"""


class IntegrationTurnGrantStore:
    def __init__(self, connection_factory=None):
        self._connection_factory = connection_factory or _default_connection

    @contextmanager
    def _cursor(self, scope):
        if type(scope) not in (Authority, TenantProject):
            _invalid()
        scope.__post_init__()
        try:
            with self._connection_factory() as conn:
                # Reused connections must be idle so LOCAL settings cannot outlive
                # this method through a caller-owned outer transaction.
                if conn.info.transaction_status != 0:
                    raise GrantStoreError("GRANT_UNSAFE_DATABASE")
                with conn.transaction():
                    from psycopg.rows import dict_row
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute("SET LOCAL statement_timeout = '1000ms'")
                        cur.execute("SET LOCAL lock_timeout = '500ms'")
                        cur.execute("SET LOCAL idle_in_transaction_session_timeout = '2000ms'")
                        cur.execute(_GUARD)
                        row = cur.fetchone()
                        if not row or row["safe"] is not True:
                            raise GrantStoreError("GRANT_UNSAFE_DATABASE")
                        cur.execute("SELECT set_config('leaf.integration_tenant_id', %s, true), "
                                    "set_config('leaf.integration_project_id', %s, true)",
                                    (scope.tenant_id, scope.project_id))
                        yield cur
        except GrantStoreError:
            raise
        except Exception:
            pass
        else:
            return
        # Raise after the handler so a raw driver error cannot survive as context.
        raise GrantStoreError("GRANT_UNAVAILABLE") from None

    def put_once(self, binding: Binding, envelope: bytes):
        if type(binding) is not Binding:
            _invalid()
        binding.__post_init__()
        envelope_bytes(envelope)
        a = binding.authority
        canonical = binding.canonical_bytes().decode("utf-8")
        with self._cursor(a) as cur:
            cur.execute("""INSERT INTO public.integration_turn_grants
                (tenant_id, project_id, session_id, turn_id, grant_id, binding,
                 created_at, expires_at, envelope)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING grant_id""",
                (a.tenant_id, a.project_id, a.session_id, a.turn_id, binding.grant_id,
                 canonical, binding.issued_at, binding.expires_at, envelope))
            if cur.fetchone():
                return "created"
            cur.execute("""SELECT binding, envelope, revoked_at FROM public.integration_turn_grants
                WHERE tenant_id=%s AND project_id=%s AND session_id=%s AND turn_id=%s""",
                (a.tenant_id, a.project_id, a.session_id, a.turn_id))
            row = cur.fetchone()
            if (row and row["binding"] == binding.to_dict() and row["revoked_at"] is None
                    and row["envelope"] is not None and bytes(row["envelope"]) == envelope):
                return "already_present"
            return "conflict"

    def load_active(self, expected_authority: Authority, now):
        finite_time(now)
        if type(expected_authority) is not Authority:
            _invalid()
        a = expected_authority
        with self._cursor(a) as cur:
            cur.execute("""SELECT binding, envelope FROM public.integration_turn_grants
                WHERE tenant_id=%s AND project_id=%s AND session_id=%s AND turn_id=%s
                AND revoked_at IS NULL AND envelope IS NOT NULL AND created_at<=%s AND expires_at>%s""",
                (a.tenant_id, a.project_id, a.session_id, a.turn_id, now, now))
            row = cur.fetchone()
            if not row:
                return None
            binding = Binding.from_dict(row["binding"])
            if binding.authority != a or not binding.issued_at <= now < binding.expires_at:
                return None
            return EncryptedGrant(binding, envelope_bytes(bytes(row["envelope"])))

    def revoke_turn(self, expected_authority: Authority, now):
        finite_time(now)
        if type(expected_authority) is not Authority:
            _invalid()
        a = expected_authority
        authority = asdict(a)
        authority["operations"] = list(a.operations)
        with self._cursor(a) as cur:
            cur.execute("""UPDATE public.integration_turn_grants
                SET revoked_at=%s, envelope=NULL
                WHERE tenant_id=%s AND project_id=%s AND session_id=%s AND turn_id=%s
                AND binding @> %s::jsonb AND revoked_at IS NULL""",
                (now, a.tenant_id, a.project_id, a.session_id, a.turn_id,
                 json.dumps(authority, sort_keys=True)))
            return cur.rowcount

    def purge_expired(self, expected_tenant_project: TenantProject, now, limit=100):
        finite_time(now)
        if type(expected_tenant_project) is not TenantProject or type(limit) is not int or not 1 <= limit <= 100:
            _invalid()
        s = expected_tenant_project
        with self._cursor(s) as cur:
            cur.execute("""WITH doomed AS (
                SELECT tenant_id, project_id, grant_id FROM public.integration_turn_grants
                WHERE tenant_id=%s AND project_id=%s AND expires_at<=%s AND envelope IS NOT NULL
                ORDER BY expires_at, grant_id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE public.integration_turn_grants g SET envelope=NULL
                FROM doomed d WHERE g.tenant_id=d.tenant_id AND g.project_id=d.project_id AND g.grant_id=d.grant_id""",
                (s.tenant_id, s.project_id, now, limit))
            return cur.rowcount
