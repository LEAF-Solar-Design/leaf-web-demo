"""Exact canonical byte binding and project capability acceptance cases."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID
import hashlib

import pytest

import checkout_capability as caps
import deps
import platform_link
import solar_project_context as service

O1, O2, P1, P2, D1, D2, V1, V2, A, B = [UUID(int=n) for n in range(1, 11)]
DIGEST = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Memory:
    """Injected canonical records, blobs and rollback-capable transactions."""

    def __init__(self):
        self.tenant = deps.TenantContext(str(O1), org_id=str(O1), subject="synthetic-editor")
        self.binding = SimpleNamespace(platform_tenant_id=O1, binding_id=A)
        self.role = "editor"
        self.project_status = "active"
        self.project_deleted_at = None
        self.artifact_status = "active"
        self.authority = "postgres_canonical"
        self.org_status = "active"
        self.blobs = {}
        self.reads = []
        self.accesses = []
        self.mutations = []
        self.checkout = None
        self.last_fence = 0
        self.now = NOW
        self.versions = {}
        self.drain = None
        for version_id, seq in ((V1, 1), (V2, 2)):
            key = f"tenants/{O1}/drawings/{D1}/v/{seq:08d}.intake.json"
            object_key = f"tenants/{O1}/drawings/{D1}/v/{seq:08d}.dwg"
            self.versions[version_id] = SimpleNamespace(
                version_id=version_id, drawing_id=D1, org_id=O1, project_id=P1, seq=seq,
                deleted_at=None, intake_ref=key, oss_object=object_key,
                provenance={"schema": "leaf.drawing-import.v1", "source": {
                    "kind": "account_upload", "tenant_id": str(O1), "drawing_id": str(D1),
                    "version": seq, "intake": {"ref": key, "sha256": DIGEST},
                    "stored_object": {"ref": object_key}}})
            self.blobs[key] = b"{}"

    def require_access(self, tenant, project_id, *, write, binding=None):
        self.accesses.append(write)
        if str(tenant) != str(O1) or project_id != P1 or self.project_status == "deleted" or self.project_deleted_at:
            raise LookupError()
        if self.role is None or (write and self.role not in {"owner", "editor"}):
            raise platform_link.ProjectSessionForbidden()
        return str(O1)

    def resolve_authority(self, org_id, project_id):
        if org_id != O1 or project_id != P1:
            raise platform_link.ProjectAuthorityNotFound()
        if self.authority != "postgres_canonical":
            raise platform_link.ProjectAuthorityRequired()
        return {"org_id": org_id, "project_id": project_id, "authority_mode": self.authority}

    def resolve_version_binding(self, org_id, project_id, version_id, *, drawing_id=None, conn=None):
        version = self.versions.get(version_id)
        if (org_id != O1 or project_id != P1 or version is None or version.deleted_at
                or self.artifact_status != "active" or (drawing_id is not None and drawing_id != D1)):
            service.refuse("CONTEXT_NOT_FOUND")
        head = max((v for v in self.versions.values() if not v.deleted_at), key=lambda v: v.seq)
        return service.VersionBinding(org_id, project_id, D1, version_id, head.version_id,
                                      version_id == head.version_id, version)

    def get(self, key):
        self.reads.append(key)
        return self.blobs[key]

    def get_checkout(self, org_id, project_id, drawing_id, *, conn=None, for_update=False):
        if org_id != O1 or project_id != P1 or drawing_id != D1:
            service.refuse("CONTEXT_NOT_FOUND")
        return service.CheckoutState(org_id, project_id, drawing_id, self.checkout, self.last_fence, self.now)

    def validate_checkout_params(self, holder, ttl_s):
        return self.original_store.validate_checkout_params(holder, ttl_s)

    def acquire_checkout(self, org_id, project_id, drawing_id, *, actor_binding_id,
                         holder, ttl_s, expected_fence, conn):
        prior = self.checkout
        if expected_fence is not None and (prior is None or prior.fence != expected_fence):
            service.refuse("CHECKOUT_STALE")
        if prior and prior.expires_at > self.now:
            if expected_fence is None or actor_binding_id != prior.holder_binding_id:
                service.refuse("CHECKOUT_CONFLICT", checkout=prior)
        self.last_fence += 1
        self.checkout = service.CheckoutLease(org_id, project_id, drawing_id, holder,
            actor_binding_id, self.now, self.now + timedelta(seconds=ttl_s), self.last_fence)
        self.mutations.append((holder, ttl_s))
        return self.checkout

    def release_checkout(self, org_id, project_id, drawing_id, *, actor_binding_id, expected_fence, conn):
        lease = self.checkout
        if lease and lease.expires_at > self.now:
            if actor_binding_id != lease.holder_binding_id or expected_fence != lease.fence:
                service.refuse("CHECKOUT_DENIED")
        self.checkout = None
        self.mutations.append("release")
        return lease is not None

    def verify_checkout(self, org_id, project_id, drawing_id, *, actor_binding_id, expected_fence, conn=None):
        if self.checkout is None:
            service.refuse("CHECKOUT_REQUIRED")
        if self.checkout.expires_at <= self.now:
            service.refuse("CHECKOUT_EXPIRED")
        if self.checkout.fence != expected_fence:
            service.refuse("CHECKOUT_STALE")
        if self.checkout.holder_binding_id != actor_binding_id:
            service.refuse("CHECKOUT_DENIED")
        return self.checkout

    def run_transaction(self, operation):
        saved = self.checkout, self.last_fence, list(self.mutations)
        try:
            return operation(object())
        except Exception:
            self.checkout, self.last_fence, self.mutations = saved
            raise

    @contextmanager
    def guard(self):
        yield self.drain

    def set_bytes(self, raw):
        version = self.versions[V2]
        self.blobs[version.intake_ref] = raw
        version.provenance["source"]["intake"]["sha256"] = hashlib.sha256(raw).hexdigest()


@pytest.fixture
def memory(monkeypatch):
    records = Memory()
    original = service.graph_store()
    records.original_store = original
    monkeypatch.setattr(service, "graph_store", lambda: records)
    canonical = SimpleNamespace(get_org=lambda oid: SimpleNamespace(status=records.org_status) if oid == O1 else None,
                                is_account_upload_source_id=original.store.is_account_upload_source_id)
    monkeypatch.setattr(platform_link, "platform_store", lambda: canonical)
    monkeypatch.setattr(platform_link, "platform_db", lambda: records)
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda tenant: records.binding)
    monkeypatch.setattr(platform_link, "require_project_access", records.require_access)
    monkeypatch.setattr(platform_link, "resolve_project_authority", records.resolve_authority)
    monkeypatch.setattr(service.write_loop, "upload_backend_for_tenant", lambda tenant: records)
    monkeypatch.setattr(service.write_loop, "drawing_mutation_refusal_guard", records.guard)
    monkeypatch.setattr(caps, "_secret", lambda: "synthetic-fixture-key-for-capability-tests")
    return records


def resolve(memory, version=V2, **kwargs):
    return service.resolve_context(memory.tenant, P1, version, **kwargs)


def assert_reason(reason, operation):
    with pytest.raises(service.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == "SIP_R1_" + reason
    assert str(exc.value) == exc.value.reason_code


def test_sip_r1_exact_version_intake(memory):
    context = resolve(memory)
    assert context.binding.drawing_id == D1
    assert context.binding.input_version_id == context.binding.head_version_id == V2
    assert context.intake_bytes == b"{}" and context.intake == {}
    assert context.intake_sha256 == DIGEST
    context.intake["independent"] = True
    assert resolve(memory).intake == {}


def test_sip_r1_historical_read(memory):
    context = resolve(memory, V1)
    assert context.binding.input_version_id == V1 and context.binding.head_version_id == V2
    assert context.binding.is_head is False
    assert memory.reads == [memory.versions[V1].intake_ref]


def test_sip_r1_derive_and_compare_artifact(memory):
    assert resolve(memory).binding.drawing_id == D1
    assert_reason("CONTEXT_NOT_FOUND", lambda: resolve(memory, drawing_id=D2))


def test_sip_r1_foreign_scope_is_unavailable(memory):
    foreign = deps.TenantContext(str(O2), subject="synthetic-foreign")
    for call in (lambda: service.resolve_context(foreign, P1, V2),
                 lambda: service.resolve_context(memory.tenant, P2, V2),
                 lambda: resolve(memory, UUID(int=99))):
        assert_reason("CONTEXT_NOT_FOUND", call)
    assert memory.reads == []


def test_sip_r1_deleted_scope_is_unavailable(memory):
    for field, value in (("project_status", "deleted"), ("project_deleted_at", NOW),
                         ("artifact_status", "inactive")):
        old = getattr(memory, field)
        setattr(memory, field, value)
        assert_reason("CONTEXT_NOT_FOUND", lambda: resolve(memory))
        setattr(memory, field, old)
    memory.versions[V2].deleted_at = NOW
    assert_reason("CONTEXT_NOT_FOUND", lambda: resolve(memory))
    assert memory.reads == []


def test_sip_r1_noncanonical_scope(memory):
    memory.authority = "legacy_sqlite"
    assert_reason("CANONICAL_AUTHORITY_REQUIRED", lambda: resolve(memory))
    assert memory.reads == []


def test_sip_r1_reader_and_editor_access(memory):
    memory.role = "read_only"
    assert resolve(memory).binding.is_head
    assert_reason("PROJECT_FORBIDDEN", lambda: resolve(memory, write=True))
    memory.role = "editor"
    assert resolve(memory, write=True).binding.is_head


def test_sip_r1_revoked_access(memory):
    resolve(memory)
    memory.reads.clear()
    memory.role = None
    assert_reason("PROJECT_FORBIDDEN", lambda: resolve(memory))
    assert memory.reads == []


def test_sip_r1_missing_intake_proof(memory):
    version = memory.versions[V2]
    saved = deepcopy(version)
    for change in (lambda: setattr(version, "intake_ref", None),
                   lambda: version.provenance["source"]["intake"].pop("sha256"),
                   lambda: version.provenance.update(schema="unsupported")):
        version.__dict__.update(deepcopy(saved.__dict__))
        change()
        assert_reason("INTAKE_PROOF_REQUIRED", lambda: resolve(memory))
    assert memory.reads == []


def test_sip_r1_reference_binding(memory):
    version = memory.versions[V2]
    saved = deepcopy(version)
    for change in (lambda: version.provenance["source"]["intake"].update(ref="foreign"),
                   lambda: version.provenance["source"].update(tenant_id=str(O2)),
                   lambda: setattr(version, "oss_object", "foreign"),
                   lambda: version.provenance["source"].update(drawing_id="../foreign")):
        version.__dict__.update(deepcopy(saved.__dict__))
        change()
        assert_reason("INTAKE_REFERENCE_INVALID", lambda: resolve(memory))
    assert memory.reads == []


def test_sip_r1_exact_byte_digest(memory):
    memory.blobs[memory.versions[V2].intake_ref] = b"{} "
    assert_reason("INTAKE_DIGEST_MISMATCH", lambda: resolve(memory))


def test_sip_r1_invalid_intake_json(memory):
    for raw in (b"\xff", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}'):
        memory.set_bytes(raw)
        assert_reason("INTAKE_INVALID", lambda: resolve(memory))


def test_sip_r1_missing_intake_bytes(memory):
    memory.blobs.clear()
    assert_reason("INTAKE_UNAVAILABLE", lambda: resolve(memory))
    assert memory.reads == [memory.versions[V2].intake_ref]


def test_sip_r1_no_standalone_head_lookup(memory, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone head lookup reached")
    import store as standalone
    for module in (standalone, service.write_loop):
        for name in ("read_intake", "intake_view", "load_manifest"):
            monkeypatch.setattr(module, name, forbidden, raising=False)
    assert resolve(memory).intake == {}


def test_sip_r1_capability_scope_separation(memory):
    scope = service.checkout_scope(P1, D1)
    token = caps.mint(memory.tenant, scope, 7)
    checkout = {"holder": "Editor", "fence": 7}
    assert caps.verify(token, memory.tenant, scope, checkout) == ("Editor", 7)
    other = deps.TenantContext(str(O1), subject="synthetic-other")
    for tenant, target in ((memory.tenant, service.checkout_scope(P2, D1)),
                           (memory.tenant, service.checkout_scope(P1, D2)),
                           (other, scope), (memory.tenant, str(D1))):
        with pytest.raises(caps.CapabilityRejected):
            caps.verify(token, tenant, target, checkout)


def test_sip_r1_admission_requires_current_verified_context(memory):
    lease, token = service.acquire_project_checkout(memory.tenant, P1, D1)
    admit = lambda version=V2: service.verify_at_admission(memory.tenant, P1, D1, version,
                                                         checkout_capability=token)
    assert admit().checkout == lease
    assert_reason("STALE_VERSION", lambda: admit(V1))
    assert_reason("CHECKOUT_DENIED", lambda: service.verify_at_admission(
        memory.tenant, P1, D1, V2, checkout_capability=None))
    memory.now = lease.expires_at
    assert_reason("CHECKOUT_EXPIRED", admit)
    memory.checkout = None
    assert_reason("CHECKOUT_REQUIRED", admit)


class VerificationCursor:
    """Exact SELECT dispatch for the real checkout verifier; no DML accepted."""

    def __init__(self, lease, *, identity_active, role):
        scope = {"org_id": O1, "project_id": P1, "drawing_id": D1}
        actor_scope = dict(scope, actor=A)
        self.expected = [
            ("SELECT project_id FROM live_projects WHERE org_id = %(org_id)s "
             "AND project_id = %(project_id)s FOR SHARE", scope, {"project_id": P1}),
            ("SELECT org_id FROM orgs WHERE org_id = %(org_id)s AND status = 'active' FOR SHARE",
             scope, {"org_id": O1}),
            ("SELECT authority_mode FROM live_project_authority_modes "
             "WHERE org_id = %(org_id)s AND project_id = %(project_id)s FOR SHARE",
             scope, {"authority_mode": "postgres_canonical"}),
            ("SELECT drawing_id FROM drawing_artifacts WHERE org_id = %(org_id)s "
             "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s "
             "AND status = 'active' FOR UPDATE", scope, {"drawing_id": D1}),
            ("SELECT binding_id FROM identity_bindings WHERE platform_tenant_id = %(org_id)s "
             "AND binding_id = %(actor)s AND status = 'active' FOR SHARE",
             actor_scope, {"binding_id": A} if identity_active else None),
        ]
        if identity_active:
            self.expected.append(
                ("SELECT role FROM project_member_bindings WHERE org_id = %(org_id)s "
                 "AND project_id = %(project_id)s AND binding_id = %(actor)s "
                 "AND status = 'active' FOR SHARE",
                 actor_scope, {"role": role} if role is not None else None))
        if identity_active and role in {"owner", "editor"}:
            self.expected.extend([
                ("SELECT * FROM project_drawing_checkouts WHERE org_id = %(org_id)s "
                 "AND project_id = %(project_id)s AND drawing_id = %(drawing_id)s FOR UPDATE",
                 scope, {"holder": lease.holder, "holder_binding_id": A,
                         "acquired_at": lease.acquired_at, "expires_at": lease.expires_at,
                         "fence": lease.fence}),
                ("SELECT clock_timestamp() AS observed_at", None, {"observed_at": NOW}),
            ])
        self.statements = []
        self.result = None
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True

    def execute(self, sql, params=None):
        assert len(self.statements) < len(self.expected), "unexpected SQL"
        expected_sql, expected_params, result = self.expected[len(self.statements)]
        assert sql == expected_sql
        assert params == expected_params
        self.statements.append((sql, dict(params) if params is not None else None))
        self.result = result

    def fetchone(self):
        return self.result


class VerificationConnection:
    def __init__(self, cursor):
        self.controlled_cursor = cursor
        self.cursor_calls = 0

    def cursor(self):
        self.cursor_calls += 1
        assert self.cursor_calls == 1
        return self.controlled_cursor

    def commit(self):
        pytest.fail("caller-owned connection was committed")

    def rollback(self):
        pytest.fail("caller-owned connection was rolled back")

    def close(self):
        pytest.fail("caller-owned connection was closed")


def _verify_real_holder(memory, monkeypatch, *, identity_active, role):
    lease = memory.checkout
    before = lease, memory.last_fence, list(memory.mutations)
    for implicit in (False, True):
        cursor = VerificationCursor(lease, identity_active=identity_active, role=role)
        connection = VerificationConnection(cursor)
        transactions = []

        def transaction(operation):
            transactions.append(connection)
            return operation(connection)

        with monkeypatch.context() as patch:
            patch.setattr(memory.original_store.db, "run_transaction", transaction)
            call = lambda: memory.original_store.verify_checkout(
                O1, P1, D1, actor_binding_id=A, expected_fence=1,
                conn=None if implicit else connection)
            if identity_active and role in {"owner", "editor"}:
                assert call() == lease
            else:
                assert_reason("PROJECT_FORBIDDEN", call)
        assert transactions == ([connection] if implicit else [])
        assert connection.cursor_calls == 1 and cursor.exited
        assert len(cursor.statements) == len(cursor.expected)
        assert (memory.checkout, memory.last_fence, memory.mutations) == before


def test_sip_r1_verify_revoked_holder(memory, monkeypatch):
    lease, _token = service.acquire_project_checkout(memory.tenant, P1, D1)
    assert lease.fence == 1 and lease.expires_at > NOW
    _verify_real_holder(memory, monkeypatch, identity_active=True, role=None)
    _verify_real_holder(memory, monkeypatch, identity_active=False, role="editor")


def test_sip_r1_verify_demoted_holder(memory, monkeypatch):
    lease, _token = service.acquire_project_checkout(memory.tenant, P1, D1)
    assert lease.fence == 1 and lease.expires_at > NOW
    _verify_real_holder(memory, monkeypatch, identity_active=True, role="read_only")


def test_sip_r1_verify_live_member(memory, monkeypatch):
    lease, _token = service.acquire_project_checkout(memory.tenant, P1, D1)
    assert lease.fence == 1 and lease.expires_at > NOW
    for role in ("editor", "owner"):
        _verify_real_holder(memory, monkeypatch, identity_active=True, role=role)


def test_sip_r1_capability_exact_grammar(memory):
    token = caps.mint(memory.tenant, service.checkout_scope(P1, D1), 1)
    assert service._well_formed_capability(token)
    for malformed in ("lco1." + "a" * 63 + "\u00e9", token + " ", token + "\n",
                      "lco1." + "A" * 64, token[:-1], token + "a",
                      "lco2." + "a" * 64, None, 1, True, b"lco1." + b"a" * 64):
        assert not service._well_formed_capability(malformed)


def test_sip_r1_admission_nonascii_proof(memory):
    lease, _token = service.acquire_project_checkout(memory.tenant, P1, D1)
    before = lease, memory.last_fence, list(memory.mutations)
    assert lease.fence == 1
    assert_reason("CHECKOUT_DENIED", lambda: service.verify_at_admission(
        memory.tenant, P1, D1, V2, checkout_capability="lco1." + "a" * 63 + "\u00e9"))
    assert (memory.checkout, memory.last_fence, memory.mutations) == before


def test_sip_r1_admission_wrong_ascii_proof(memory):
    lease, token = service.acquire_project_checkout(memory.tenant, P1, D1)
    before = lease, memory.last_fence, list(memory.mutations)
    assert lease.fence == 1
    wrong = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert service._well_formed_capability(wrong)
    assert_reason("CHECKOUT_DENIED", lambda: service.verify_at_admission(
        memory.tenant, P1, D1, V2, checkout_capability=wrong))
    assert (memory.checkout, memory.last_fence, memory.mutations) == before
