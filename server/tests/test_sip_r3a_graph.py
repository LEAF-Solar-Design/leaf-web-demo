"""Dependency-isolated canonical Solar graph acceptance cases."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_tools
from leaf_platform.models import DrawingVersion
from solar_design_graph import GraphValidationError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNITS = {"drawing_units": "ft", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
          0, 0, 1, 0, 0, 0, 0, 1], "elevation_datum": "unknown", "crs": None}
INTAKE = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [],
          "blockdefs": [], "geodata": None, "custom": {"keep": [1, 2, 3]}}


class Memory:
    """Records/blob double; no claim of PostgreSQL authority or rollback."""
    def __init__(self):
        self.org, self.project, self.drawing, self.actor, self.job = [uuid4() for _ in range(5)]
        self.versions, self.blobs, self.fingerprints = {}, {}, {}
        self.reads, self.writes, self.calls = [], [], []
        self.parent = DrawingVersion(uuid4(), self.drawing, self.project, self.org, 1,
                                     created_at=NOW)
        self.set_parent(INTAKE)
        self.lease = project.CheckoutLease(self.org, self.project, self.drawing, "Editor",
                                          self.actor, NOW, NOW + timedelta(hours=1), 7)
        self.head = self.parent.version_id

    def set_parent(self, intake):
        raw = local.canonical_bytes(intake)
        source = str(uuid4())
        key = f"tenants/{self.org}/drawings/{source}/v/00000001.intake.json"
        self.parent.intake_ref, self.parent.oss_object = key, key[:-12] + ".dwg"
        self.parent.provenance = {"schema": "leaf.drawing-import.v1", "source": {
            "kind": "account_upload", "tenant_id": str(self.org), "drawing_id": source,
            "version": 1, "stored_object": {"ref": self.parent.oss_object},
            "intake": {"ref": key, "sha256": sha256(raw).hexdigest()}}}
        self.blobs[key] = raw
        self.versions[self.parent.version_id] = self.parent

    def get(self, key):
        self.reads.append(key)
        return self.blobs[key]

    def put_if_absent_or_verify(self, key, raw):
        self.writes.append(key)
        assert key not in self.blobs or self.blobs[key] == raw
        self.blobs[key] = raw

    def resolve(self, org, proj, version, *, drawing_id=None, conn=None):
        if ((org, proj, drawing_id) != (self.org, self.project, self.drawing)
                or version not in self.versions):
            project.refuse("CONTEXT_NOT_FOUND")
        return project.VersionBinding(org, proj, drawing_id, version, self.head,
                                      version == self.head, self.versions[version])

    def context(self, version=None):
        binding = self.resolve(self.org, self.project, version or self.parent.version_id,
                               drawing_id=self.drawing)
        return project._read_intake(binding)

    def publish(self, org, proj, drawing, **kwargs):
        self.calls.append((org, proj, drawing, kwargs))
        key = "sip-r2:" + str(kwargs["request_id"])
        for version in self.versions.values():
            if version.idempotency_key == key:
                return version
        kwargs["before_new"]()
        proof = project.graph_store()._publication_proof(org, proj, drawing,
            kwargs["expected_parent_version_id"], kwargs["actor_binding_id"],
            kwargs["expected_fence"], kwargs["request_id"], kwargs["intake_sha256"],
            self.versions[kwargs["expected_parent_version_id"]].oss_object)
        version = DrawingVersion(uuid4(), drawing, proj, org, len(self.versions) + 1,
            oss_object=proof["base_object_ref"], intake_ref=proof["intake"]["ref"],
            provenance=proof, idempotency_key=key, created_at=NOW)
        self.versions[version.version_id] = version
        self.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(proof)
        self.head = version.version_id
        return version

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        assert sql.startswith("SELECT import_fingerprint FROM drawing_versions")
        assert params[:2] == (self.org, self.project)
        self.selected = params[2]

    def fetchone(self):
        return {"import_fingerprint": self.fingerprints[self.selected]}


@pytest.fixture
def memory(monkeypatch):
    state = Memory()
    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant", lambda tenant: state)
    monkeypatch.setattr(project.graph_store(), "resolve_version_binding", state.resolve)
    monkeypatch.setattr(project.graph_store(), "publish_version", state.publish)
    monkeypatch.setattr(project, "_access", lambda tenant, proj, write: (state.org, state.actor))
    return state


def prepare(s, params=None, *, seed=True, context=None, **changes):
    args = dict(checkout=s.lease, job_id=s.job, attempt=1,
                tool_manifest_sha256=deps.catalog_tool_digest(solar_tools.trusted_record("solar-settings")))
    args.update(changes)
    context = context or s.context()
    if params is None:
        params = {"expected_rev": 0, "changes": {"panels_in_sequence": 3},
                  "initialize": {"schema_version": 1, "source_intake_sha256": context.intake_sha256,
                                 "units": deepcopy(UNITS)}}
    if seed:
        return adapter.prepare_project_graph_seed(context, params, **args)
    return adapter.prepare_project_graph_commit(context, "solar-settings", params, **args)


def publish(s, prepared):
    return adapter.publish_project_graph_commit(prepared, actor_binding_id=s.actor, conn=s)


def proof(s, result, prepared):
    return adapter.project_graph_commit_provenance(str(s.org), result, expected=prepared, conn=s)


def refused(code, operation):
    with pytest.raises((project.ProjectContextError, GraphValidationError)) as exc:
        operation()
    assert getattr(exc.value, "reason_code", getattr(exc.value, "code", None)) == code


def test_sip_r3a_context_mapping(memory):
    prepared = prepare(memory)
    result = publish(memory, prepared)
    context = adapter.resolve_project_graph_context(memory.context(UUID(result["output_version_id"])))
    assert context.project_id == memory.project
    assert context.drawing_id == memory.drawing
    assert context.graph_project_id == result["graph_project_id"] != str(memory.project)
    assert context.graph["project"]["id"] == prepared.request["graph_project_id"]
    imported = context.intake
    imported_id = "leaf:project:" + str(uuid4())
    imported["solar_design_graph"]["project"]["id"] = imported_id
    imported["solar_design_graph_sha256"] = local.digest(imported["solar_design_graph"])
    memory.set_parent(imported)
    changed = prepare(memory, {"expected_rev": 1, "changes": {"num_mppt": 2}}, seed=False)
    assert json.loads(changed.output_intake_bytes)["solar_design_graph"]["project"]["id"] == imported_id


def test_sip_r3a_context_graphless(memory):
    context = adapter.resolve_project_graph_context(memory.context())
    assert context.graph is None and context.graph_project_id is None
    assert context.intake == INTAKE and context.created_at == "2026-01-01T00:00:00Z"
    memory.parent.created_at = NOW.astimezone(timezone(timedelta(hours=-6)))
    assert adapter.resolve_project_graph_context(memory.context()).created_at == context.created_at
    refused("SIP_R3_PROOF_REJECTED", lambda: adapter.resolve_project_graph_context({}))
    verified = memory.context()
    refused("SIP_R3_PROOF_REJECTED", lambda: adapter.resolve_project_graph_context(
        replace(verified, binding=replace(verified.binding, project_id=uuid4()))))


def test_sip_r3a_context_graph_digest(memory):
    intake = json.loads(prepare(memory).output_intake_bytes)
    intake["solar_design_graph_sha256"] = "0" * 64
    memory.set_parent(intake)
    refused("GRAPH_DIGEST_MISMATCH", lambda: adapter.resolve_project_graph_context(memory.context()))


def test_sip_r3a_context_content_digest(memory):
    verified = memory.context()
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda: adapter.resolve_project_graph_context(
        replace(verified, intake_bytes=verified.intake_bytes + b" ")))
    verified.intake["foreign"] = True
    refused("SIP_R3_PROOF_REJECTED", lambda: adapter.resolve_project_graph_context(verified))


def test_sip_r3a_seed_revision_one(memory):
    prepared = prepare(memory)
    result = publish(memory, prepared)
    graph = memory.context(UUID(result["output_version_id"])).intake["solar_design_graph"]
    assert graph["rev"] == 1 and graph["parent_rev"] == 0
    assert result["before_rev"] is None and result["seed_base_rev"] == 0
    assert graph["project"]["provenance"]["created_at"] == "2026-01-01T00:00:00Z"
    assert result["output_version_id"] != str(memory.parent.version_id)


def test_sip_r3a_settings_revision_two(memory):
    first = publish(memory, prepare(memory))
    params = {"expected_rev": 1, "changes": {"num_mppt": 2}, "drawing_id": str(memory.drawing)}
    prepared = prepare(memory, params, seed=False, context=memory.context(UUID(first["output_version_id"])))
    result = publish(memory, prepared)
    graph = memory.context(UUID(result["output_version_id"])).intake["solar_design_graph"]
    assert graph["rev"] == 2 and graph["parent_rev"] == 1
    assert graph["project"]["id"] == first["graph_project_id"]
    assert "drawing_id" not in prepared.request["parameters"]
    assert graph["settings"]["num_mppt"] == 2


def test_sip_r3a_seed_request_validation(memory):
    for initialize in (None, {}, {"schema_version": True}):
        refused("INVALID_SEED_REQUEST", lambda: prepare(memory,
            {"expected_rev": 0, "changes": {"num_mppt": 2}, "initialize": initialize}))


def test_sip_r3a_seed_source_hash(memory):
    refused("SOURCE_HASH_MISMATCH", lambda: prepare(memory, {"expected_rev": 0,
        "changes": {"num_mppt": 2}, "initialize": {"schema_version": 1,
        "source_intake_sha256": "0" * 64, "units": deepcopy(UNITS)}}))


def test_sip_r3a_seed_existing_graph(memory):
    first = publish(memory, prepare(memory))
    refused("GRAPH_ALREADY_EMBEDDED", lambda: prepare(memory,
        context=memory.context(UUID(first["output_version_id"]))))


def test_sip_r3a_seed_partial_companion(memory):
    for field in ("solar_design_graph", "solar_design_graph_sha256"):
        memory.set_parent({**INTAKE, field: {}})
        refused("INVALID_SEED_PARENT", lambda: prepare(memory))


def test_sip_r3a_ordinary_graph_required(memory):
    refused("GRAPH_NOT_EMBEDDED", lambda: prepare(memory,
        {"expected_rev": 0, "changes": {"num_mppt": 2}}, seed=False))


def test_sip_r3a_revision_and_cancel_refusals(memory):
    first = publish(memory, prepare(memory))
    ctx = memory.context(UUID(first["output_version_id"]))
    for revision in (0, True, 1.0, None):
        refused("STALE_GRAPH_REVISION", lambda: prepare(memory,
            {"expected_rev": revision, "changes": {"num_mppt": 2}}, seed=False, context=ctx))
    refused("GRAPH_COMMIT_CANCELLED", lambda: prepare(memory,
        {"expected_rev": 1, "cancel": True}, seed=False, context=ctx))
    refused("DRAWING_ID_CONFLICT", lambda: prepare(memory,
        {"drawing_id": str(uuid4()), "expected_rev": 1, "changes": {"num_mppt": 2}},
        seed=False, context=ctx))
    assert len(memory.versions) == 2


def test_sip_r3a_request_binding(memory):
    one = prepare(memory)
    assert one.request_sha256 == sha256(local.canonical_bytes(one.request)).hexdigest()
    assert one.request_id == uuid5(NAMESPACE_URL, adapter.SCHEMA + ":" + one.request_sha256)
    assert one.request["checkout_fence"] == "7"
    for changes in ({"job_id": uuid4()}, {"attempt": 2},
                    {"checkout": replace(memory.lease, fence=8)},
                    {"checkout": replace(memory.lease, holder_binding_id=uuid4())}):
        other = prepare(memory, **changes)
        assert other.request_id != one.request_id
        assert other.output_intake_bytes == one.output_intake_bytes
    for attempt in (True, 0):
        refused("SIP_R3_PROOF_REJECTED", lambda: prepare(memory, attempt=attempt))
    params = one.request["parameters"]
    params["changes"]["num_mppt"] = 0  # same result, distinct parameter identity
    assert prepare(memory, params).request_id != one.request_id


def test_sip_r3a_tool_manifest(memory):
    refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: prepare(memory, tool_manifest_sha256="sha256:" + "0" * 64))
    assert prepare(memory).request["tool_manifest_sha256"].startswith("sha256:")


def test_sip_r3a_unsupported_tool(memory):
    for tool in ("solar-commit-solve", "unknown", {}):
        refused("SIP_R3_TOOL_UNSUPPORTED", lambda: adapter.prepare_project_graph_commit(
            memory.context(), tool, {}, checkout=memory.lease, job_id=memory.job, attempt=1,
            tool_manifest_sha256="sha256:" + "0" * 64))


def test_sip_r3a_canonical_source_intake(memory, monkeypatch):
    first = publish(memory, prepare(memory))
    context = adapter.resolve_project_graph_context(memory.context(UUID(first["output_version_id"])))
    def forbidden(*args, **kwargs):
        pytest.fail("standalone resolver called")
    for module, names in ((local, ("_source_intake", "_resolve_trusted")),
                          (local.store, ("resolve_version_entry", "load_manifest")),
                          (local.write_loop, ("read_intake", "intake_view"))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden, raising=False)
    source = adapter._canonical_source_intake(context)
    assert source == INTAKE
    source["custom"]["keep"].append(4)
    assert adapter._canonical_source_intake(context) == INTAKE
    intake = context.intake
    intake["solar_design_graph_sha256"] = "0" * 64
    raw = local.canonical_bytes(intake)
    bad = replace(context, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    refused("GRAPH_DIGEST_MISMATCH", lambda: adapter._canonical_source_intake(bad))


def test_sip_r3a_prepared_immutability(memory):
    verified = memory.context()
    prepared = prepare(memory, context=verified)
    original = prepared.request_bytes, prepared.output_intake_bytes, prepared.receipt_bytes
    request = prepared.request
    request["parameters"]["changes"]["panels_in_sequence"] = 99
    verified.intake["custom"]["keep"].append(99)
    assert (prepared.request_bytes, prepared.output_intake_bytes, prepared.receipt_bytes) == original
    assert prepare(memory).request_bytes == prepared.request_bytes
    params = prepared.request["parameters"]
    another = prepare(memory, params)
    params["initialize"]["units"]["wcs_to_ucs"][0] = 99
    params["changes"]["panels_in_sequence"] = 99
    assert another.request_bytes == prepared.request_bytes


def test_sip_r3a_publisher_arguments(memory, monkeypatch):
    prepared = prepare(memory)
    publish(memory, prepared)
    org, proj, drawing, args = memory.calls[-1]
    assert (org, proj, drawing) == (memory.org, memory.project, memory.drawing)
    assert set(args) == {"expected_parent_version_id", "actor_binding_id", "expected_fence",
                         "request_id", "intake_sha256", "conn", "before_new"}
    assert args["expected_parent_version_id"] == memory.parent.version_id
    assert args["actor_binding_id"] == memory.actor and args["expected_fence"] == 7
    assert args["request_id"] == prepared.request_id and args["conn"] is memory
    assert args["intake_sha256"] == prepared.output_intake_sha256
    refused("SIP_R3_PROOF_REJECTED", lambda: adapter.publish_project_graph_commit(
        prepared, actor_binding_id=uuid4(), conn=memory))
    mutated = replace(prepared, output_intake_bytes=prepared.output_intake_bytes + b" ")
    before = len(memory.versions)
    # Use a different request so the double enters the callback instead of replay.
    fresh = prepare(memory, attempt=2)
    mutated = replace(fresh, output_intake_bytes=mutated.output_intake_bytes)
    refused("SIP_R3_PROOF_REJECTED", lambda: publish(memory, mutated))
    assert len(memory.versions) == before
    events = []
    def mutation(operation):
        events.append("guard")
        return operation(memory)
    monkeypatch.setattr(project, "_mutation", mutation)
    assert adapter.run_project_graph_seed(str(memory.org), memory.context(),
        prepared.request["parameters"], checkout=memory.lease, job_id=memory.job, attempt=1,
        tool_manifest_sha256=prepared.request["tool_manifest_sha256"])["output_version_id"] == str(memory.head)
    assert events == ["guard"]


def test_sip_r3a_replay_no_blob_io(memory, monkeypatch):
    prepared = prepare(memory)
    first = publish(memory, prepared)
    reads, writes, count = len(memory.reads), len(memory.writes), len(memory.versions)
    def forbidden(*args, **kwargs):
        pytest.fail("replay performed content IO or execution")
    monkeypatch.setattr(memory, "get", forbidden)
    monkeypatch.setattr(memory, "put_if_absent_or_verify", forbidden)
    monkeypatch.setattr(local, "_load_builtin", forbidden)
    assert publish(memory, prepared) == first and "replayed" not in first
    assert (len(memory.reads), len(memory.writes), len(memory.versions)) == (reads, writes, count)


def test_sip_r3a_proof_roundtrip(memory):
    seeded = prepare(memory)
    first = publish(memory, seeded)
    settings = prepare(memory, {"expected_rev": 1, "changes": {"num_mppt": 2}},
                       seed=False, context=memory.context(UUID(first["output_version_id"])))
    second = publish(memory, settings)
    for prepared, result in ((seeded, first), (settings, second)):
        assert proof(memory, result, prepared)["output_version_id"] == result["output_version_id"]
        assert proof(memory, result, prepared.request)["request_sha256"] == prepared.request_sha256
    parent_id = UUID(first["output_version_id"])
    saved = memory.fingerprints[parent_id]
    memory.fingerprints[parent_id] = "0" * 64
    refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, second, settings))
    memory.fingerprints[parent_id] = saved


def test_sip_r3a_proof_identity_tampering(memory):
    prepared = prepare(memory)
    result = publish(memory, prepared)
    for field in result:
        changed = deepcopy(result)
        if field == "parameters":
            changed[field]["changes"]["panels_in_sequence"] = 4
        elif type(changed[field]) is bool:
            changed[field] = not changed[field]
        elif type(changed[field]) is int:
            changed[field] += 1
        elif field == "output_version_id":
            changed[field] = str(memory.parent.version_id)
        else:
            changed[field] = "tampered"
        refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, changed, prepared))
    changed = deepcopy(result)
    changed["output_version_id"] = str(uuid4())
    refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, changed, prepared))
    for field in ("job_id", "attempt", "tool_manifest_sha256", "parameters"):
        expected = prepared.request
        expected[field] = {"job_id": str(uuid4()), "attempt": 2,
            "tool_manifest_sha256": "sha256:" + "0" * 64,
            "parameters": {"expected_rev": 0}}[field]
        refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, result, expected))


def test_sip_r3a_proof_stored_companion_tampering(memory):
    prepared = prepare(memory)
    result = publish(memory, prepared)
    version = memory.versions[UUID(result["output_version_id"])]
    original = deepcopy(version)
    original_bytes = memory.blobs[version.intake_ref]
    memory.fingerprints[version.version_id] = "0" * 64
    refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, result, prepared))
    memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)
    for change in (lambda intake: intake["custom"]["keep"].append(99),
                   lambda intake: intake["solar_design_graph"]["settings"].update(num_mppt=9)):
        intake = json.loads(original_bytes)
        change(intake)
        intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
        raw = local.canonical_bytes(intake)
        digest = sha256(raw).hexdigest()
        version.__dict__.update(deepcopy(original.__dict__))
        version.intake_ref = project.graph_store().publication_intake_key(memory.org, memory.project, memory.drawing, digest)
        version.provenance["intake"] = {"ref": version.intake_ref, "sha256": digest}
        memory.blobs[version.intake_ref] = raw
        memory.fingerprints[version.version_id] = project.graph_store()._publication_fingerprint(version.provenance)
        refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, result, prepared))
        forged = deepcopy(result)
        forged["graph_sha256"] = local.digest(intake["solar_design_graph"])
        forged["output_intake_sha256"] = digest
        refused("SIP_R3_PROOF_REJECTED", lambda: proof(memory, forged, prepared))
    version.__dict__.update(deepcopy(original.__dict__))
    memory.blobs[version.intake_ref] = original_bytes + b" "
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda: proof(memory, result, prepared))


def test_sip_r3a_legacy_bytes():
    raw = Path(local.__file__).read_bytes()
    prefix = raw[:raw.index(b"\n\ndef run_project_graph_seed")]
    assert sha256(prefix).hexdigest() == "889f62cc4bfee44532ae6a6170852bc340bf5106a664c3c89d06c1a672dde005"
    source_hash = sha256(local.canonical_bytes(INTAKE)).hexdigest()
    assert source_hash == "77e8d2aaf6f3b2eaff320e4ff2e55ccdaa4237a9a9e9adca5ef23a9e32c0d8ef"
    base = local.new_empty_graph(tenant_id="fixture-tenant", drawing_id="solar",
        source_hash=source_hash, units=deepcopy(UNITS), created_at="2026-01-01T00:00:00Z")
    params = {"expected_rev": 0, "changes": {"panels_in_sequence": 3}}
    after = local._load_builtin("solar-settings").run(base, params)
    assert local.digest(after) == "05a6af8bb6017968a571491ec872b52be3368f63d72df976f14c14833fb2c9c4"
    assert sha256(local.canonical_bytes(local.version_companion(INTAKE, None, after))).hexdigest() == "03d44f697f5cb20f1045243b2037cc913d0e6f508d4b7862a09312e4be51184a"
    ordinary = local._load_builtin("solar-settings").run(after, {"expected_rev": 1, "changes": {"num_mppt": 2}})
    assert local.digest(ordinary) == "4dbbcbaf991d69cbbeda760a1d202dff1a9b91754fcb85526ee5ca79b7fb0f72"
    assert sha256(local.canonical_bytes(local.version_companion(json.loads(local.canonical_bytes(local.version_companion(INTAKE, None, after))), after, ordinary))).hexdigest() == "9f9fefaa1f83113c8aecc0c12d64ba24d66f7d916f5aeb976522b50d6fdd3724"
    assert local.request_digest("solar-settings", "solar", 1, {**params, "initialize": {
        "schema_version": 1, "source_intake_sha256": source_hash, "units": deepcopy(UNITS)}}) == "ec73fc8a161f512cf3b8c554f4227b6f7547727a80f238e38b4f989f56b6b52c"


def test_sip_r3a_additive_entrypoints_no_activation(memory, monkeypatch):
    for name in ("run_project_graph_seed", "run_project_graph_commit", "project_graph_commit_provenance"):
        sentinel = object()
        monkeypatch.setattr(adapter, name, lambda *args, **kwargs: sentinel)
        assert getattr(local, name)("forwarded") is sentinel
    assert adapter.SUPPORTED_TOOLS == {"solar-settings", "solar-panels-from-drawing",
        "solar-size-strings", "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
        "solar-string-add", "solar-assign-equipment"}
    root = Path(local.__file__).parent
    assert "solar_project_graph" not in (root / "canonical_worker.py").read_text(encoding="utf-8")
    assert "canonical_only" not in solar_tools.trusted_record("solar-settings")
