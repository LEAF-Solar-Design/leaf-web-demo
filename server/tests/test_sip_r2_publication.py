"""Eight offline acceptance rows for publication proof and adapter contracts."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import solar_project_context as service
from routers import project_drawings as routes
from leaf_platform.models import DrawingVersion


@pytest.fixture
def publication(monkeypatch):
    org, project, drawing, parent, actor, request, output = [uuid4() for _ in range(7)]
    raw = b'{"panels": []}\n'
    digest = sha256(raw).hexdigest()
    graph = service.graph_store()
    proof = graph._publication_proof(org, project, drawing, parent, actor, 7,
                                     request, digest, "inherited.dwg")
    version = DrawingVersion(output, drawing, project, org, 2, oss_object="inherited.dwg",
        intake_ref=proof["intake"]["ref"], provenance=proof,
        idempotency_key="sip-r2:" + str(request), created_by=str(actor))
    binding = service.VersionBinding(org, project, drawing, output, output, True, version)
    state = SimpleNamespace(org=org, project=project, drawing=drawing, parent=parent,
        actor=actor, request=request, digest=digest, raw=raw, version=version, binding=binding,
        blobs={version.intake_ref: raw}, reads=[], accesses=[], calls=[], events=[], drain=None)

    def backend(tenant):
        assert tenant == str(org)

        def get(key):
            state.reads.append(key)
            return state.blobs[key]
        return SimpleNamespace(get=get)

    monkeypatch.setattr(service.write_loop, "upload_backend_for_tenant", backend)
    return state


def refused(code, operation):
    with pytest.raises(service.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == code


def invoke(s, **changes):
    args = dict(expected_parent_version_id=s.parent, expected_fence=7,
                request_id=s.request, intake_sha256=s.digest)
    args.update(changes)
    return service.publish_project_version(str(s.org), s.project, s.drawing, **args)


def wire_adapter(s, monkeypatch):
    link = service.platform_link
    monkeypatch.setattr(link, "resolve_caller_binding", lambda tenant:
        SimpleNamespace(platform_tenant_id=s.org, binding_id=s.actor))

    def access(tenant, project, *, write, binding):
        assert tenant == str(s.org) and project == s.project
        assert write is True and binding.binding_id == s.actor
        s.accesses.append(binding.binding_id)

    monkeypatch.setattr(link, "require_project_access", access)
    monkeypatch.setattr(link, "platform_store", lambda:
        SimpleNamespace(get_org=lambda org: SimpleNamespace(status="active")))
    monkeypatch.setattr(link, "resolve_project_authority", lambda org, project: None)

    @contextmanager
    def guard():
        s.events.append("guard-enter")
        try:
            yield s.drain
        finally:
            s.events.append("guard-exit")

    connection = object()

    def transaction(operation):
        assert s.events[-1] == "guard-enter"
        result = operation(connection)
        s.events.append("commit")
        return result

    def publish(org, project, drawing, **kwargs):
        assert (org, project, drawing) == (s.org, s.project, s.drawing)
        before_new = kwargs.pop("before_new")
        assert callable(before_new)
        before_new()
        assert kwargs == dict(expected_parent_version_id=s.parent, actor_binding_id=s.actor,
            expected_fence=7, request_id=s.request, intake_sha256=s.digest, conn=connection)
        s.calls.append(kwargs)
        return s.version

    monkeypatch.setattr(service.write_loop, "drawing_mutation_refusal_guard", guard)
    monkeypatch.setattr(link, "platform_db", lambda: SimpleNamespace(run_transaction=transaction))
    monkeypatch.setattr(service.graph_store(), "publish_version", publish)


def test_sip_r2_canonical_proof_readback(publication):
    s = publication
    result = service._read_intake(s.binding)
    assert result.binding is s.binding
    assert result.intake_bytes == s.raw and result.intake == {"panels": []}
    assert result.intake_sha256 == s.digest and result.intake_ref == s.version.intake_ref
    assert s.reads == [s.version.intake_ref]


def test_sip_r2_scope_key_rejection(publication):
    s = publication
    original = deepcopy(s.version.provenance)
    for field in ("organization_id", "project_id", "drawing_id"):
        s.version.provenance = deepcopy(original)
        s.version.provenance[field] = str(uuid4())
        refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))
    for field in ("parent_version_id", "request_id", "actor_binding_id"):
        s.version.provenance = deepcopy(original)
        s.version.provenance[field] = UUID(original[field]).hex
        refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))
    for fence in (True, 0, "0", "01", "9223372036854775808"):
        s.version.provenance = deepcopy(original)
        s.version.provenance["checkout_fence"] = fence
        refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))
    for field, value in (("request_id", str(uuid4())), ("base_object_ref", "other.dwg")):
        s.version.provenance = deepcopy(original)
        s.version.provenance[field] = value
        refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))
    s.version.provenance = deepcopy(original)
    s.version.provenance["intake"]["ref"] = "../foreign.json"
    refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))
    s.version.provenance = {"schema": "unsupported"}
    refused("SIP_R1_INTAKE_PROOF_REQUIRED", lambda: service._read_intake(s.binding))
    assert s.reads == []


def test_sip_r2_exact_byte_digest_failures(publication):
    s = publication
    s.blobs[s.version.intake_ref] = b'{"panels": []}'  # Same JSON, different bytes.
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda: service._read_intake(s.binding))
    s.blobs[s.version.intake_ref] = "not bytes"
    refused("SIP_R1_INTAKE_UNAVAILABLE", lambda: service._read_intake(s.binding))
    s.blobs.clear()
    refused("SIP_R1_INTAKE_UNAVAILABLE", lambda: service._read_intake(s.binding))


def test_sip_r2_strict_json_rejection(publication):
    s = publication
    for raw in (b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                b'{"x":1e999}', b'{"x":', b'\xff'):
        digest = sha256(raw).hexdigest()
        key = service.graph_store().publication_intake_key(s.org, s.project, s.drawing, digest)
        s.version.intake_ref = key
        s.version.provenance["intake"] = {"ref": key, "sha256": digest}
        s.blobs[key] = raw
        refused("SIP_R1_INTAKE_INVALID", lambda: service._read_intake(s.binding))


def test_sip_r2_publication_access_guard_binding(publication, monkeypatch):
    s = publication
    wire_adapter(s, monkeypatch)
    assert invoke(s) is s.version
    assert s.accesses == [s.actor] and len(s.calls) == 1
    assert s.events == ["guard-enter", "commit", "guard-exit"]
    s.drain = "closed"
    refused("SIP_R1_WRITES_DRAINED", lambda: invoke(s))
    assert len(s.calls) == 1
    s.drain = None
    s.blobs.clear()
    refused("SIP_R1_INTAKE_UNAVAILABLE", lambda: invoke(s))
    assert len(s.calls) == 1

    def denied(*args, **kwargs):
        raise service.platform_link.ProjectSessionForbidden()

    monkeypatch.setattr(service.platform_link, "require_project_access", denied)
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda: invoke(s))
    assert len(s.calls) == 1


def test_sip_r2_replay_passthrough(publication, monkeypatch):
    s = publication
    wire_adapter(s, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("publication replay must not repeat admission/head checks")

    monkeypatch.setattr(service, "verify_at_admission", forbidden)
    monkeypatch.setattr(service.graph_store(), "resolve_version_binding", forbidden)
    first = invoke(s)
    before = first.to_dict()
    assert invoke(s) is first and first.to_dict() == before
    assert "replayed" not in before and len(s.calls) == 2
    assert s.accesses == [s.actor, s.actor]


def test_sip_r2_refusal_mapping():
    cases = {
        "SIP_R2_PUBLICATION_PARAMS_INVALID": 400, "SIP_R2_IDEMPOTENCY_CONFLICT": 409,
        "SIP_R2_PUBLICATION_REQUIRED": 409, "SIP_R1_CONTEXT_NOT_FOUND": 404,
        "SIP_R1_PROJECT_FORBIDDEN": 403, "SIP_R1_CHECKOUT_DENIED": 403,
        "SIP_R1_CHECKOUT_EXPIRED": 409, "SIP_R1_CHECKOUT_STALE": 409,
        "SIP_R1_STALE_VERSION": 409, "SIP_R1_WRITES_DRAINED": 503,
    }
    for reason, status in cases.items():
        def operation():
            raise service.ProjectContextError(reason)
        response = routes._dispatch(operation)
        assert response.status_code == status
        assert json.loads(response.body)["error"]["reason_code"] == reason
    for reason in ("SIP_R2_CONTEXT_NOT_FOUND", "CONTEXT_NOT_FOUND", "SIP_R1_UNKNOWN"):
        def operation():
            raise service.ProjectContextError(reason)
        response = routes._dispatch(operation)
        assert response.status_code == 500
        assert json.loads(response.body)["error"]["reason_code"] == "SIP_R1_INTERNAL"


def test_sip_r2_unchanged_import_proof_reading(publication):
    s = publication
    source = str(uuid4())
    key = f"tenants/{s.org}/drawings/{source}/v/00000007.intake.json"
    object_key = key[:-12] + ".dwg"
    s.version.intake_ref, s.version.oss_object = key, object_key
    s.version.provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(s.org), "drawing_id": source,
        "version": 7, "stored_object": {"ref": object_key},
        "intake": {"ref": key, "sha256": s.digest}}}
    s.blobs[key] = s.raw
    assert service._read_intake(s.binding).intake_bytes == s.raw
    s.version.provenance["source"]["tenant_id"] = str(uuid4())
    refused("SIP_R1_INTAKE_REFERENCE_INVALID", lambda: service._read_intake(s.binding))


def test_sip_r2_adapter_classifies_replay_before_reading_intake(publication, monkeypatch):
    s = publication
    wire_adapter(s, monkeypatch)
    stored = {s.request: (s.digest, s.version)}

    def publish(org, project, drawing, **kwargs):
        assert (org, project, drawing) == (s.org, s.project, s.drawing)
        before_new = kwargs["before_new"]
        assert callable(before_new)
        prior = stored.get(kwargs["request_id"])
        if prior is not None:
            if kwargs["intake_sha256"] != prior[0]:
                raise service.ProjectContextError("SIP_R2_IDEMPOTENCY_CONFLICT")
            return prior[1]
        before_new()
        stored[kwargs["request_id"]] = (kwargs["intake_sha256"], s.version)
        return s.version

    def backend(tenant):
        assert tenant == str(s.org)

        def get(key):
            s.reads.append(key)
            raise RuntimeError("intake backend unavailable")

        return SimpleNamespace(get=get)

    monkeypatch.setattr(service.graph_store(), "publish_version", publish)
    monkeypatch.setattr(service.write_loop, "upload_backend_for_tenant", backend)
    assert invoke(s) is s.version
    assert s.reads == []
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: invoke(s, intake_sha256="b" * 64))
    assert s.reads == []
    refused("SIP_R1_INTAKE_UNAVAILABLE", lambda: invoke(s, request_id=uuid4()))
    assert s.reads == [s.version.intake_ref]


def test_sip_r2_adapter_passes_before_new(publication, monkeypatch):
    s = publication
    wire_adapter(s, monkeypatch)
    callbacks = []

    def publish(org, project, drawing, **kwargs):
        assert (org, project, drawing) == (s.org, s.project, s.drawing)
        before_new = kwargs["before_new"]
        assert callable(before_new)
        callbacks.append(before_new)
        assert s.reads == []
        before_new()
        assert s.reads == [s.version.intake_ref]
        return s.version

    monkeypatch.setattr(service.graph_store(), "publish_version", publish)
    assert invoke(s) is s.version
    assert len(callbacks) == 1 and s.reads == [s.version.intake_ref]
