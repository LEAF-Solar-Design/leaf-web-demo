"""Canonical Solar graph preparation, fenced publication, and stored-content proof.

This library has no dispatch registration. Prepared bytes are the retry identity;
only R2 decides whether a publication is new before any content is reread.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_sizing as sizing
import solar_sizing_client as cloud
import solar_tools
from solar_design_graph import GraphValidationError, _bounded_json, validate_graph
from solar_graph_seed import new_empty_graph, validate_seed_request

SCHEMA = "leaf.solar-project-graph-commit.v1"
ADAPTER_KIND = "project-local-graph-commit"
SUPPORTED_TOOLS = frozenset((
    "solar-settings",
    "solar-panels-from-drawing",
    "solar-size-strings",
    "solar-combiners",
    "solar-feeders",
    "solar-homeruns",
    "solar-schedule",
    "solar-string-add",
    "solar-assign-equipment",
    "solar-string-multi-add",
    "solar-string-conductors",
))
# Sizing runs only its pure manual-global branch here; every other mode needs stored
# service evidence this path does not have and is refused before the builtin loads.
SIZING_TOOL = "solar-size-strings"
MANUAL_SIZING_MODE = "manual-global"
# Tools that create entities. Their IDs and creation times derive from the exact parent
# and normalized request, so preparation, publication and proof regenerate equal bytes.
STRING_TOOL = "solar-string-add"
STRING_OPERATION = "add-string"
CREATION_TOOLS = frozenset((STRING_TOOL, "solar-assign-equipment", "solar-string-multi-add"))
CREATION_SCHEMA = "leaf.solar-project-creation.v1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _reject():
    raise project.ProjectContextError("SIP_R3_PROOF_REJECTED")


@dataclass(frozen=True)
class ProjectGraphContext:
    organization_id: UUID
    project_id: UUID
    drawing_id: UUID
    parent_version_id: UUID
    intake_bytes: bytes
    intake_sha256: str
    created_at: str

    @property
    def intake(self):
        return json.loads(self.intake_bytes)

    @property
    def graph(self):
        return self.intake.get("solar_design_graph")

    @property
    def graph_sha256(self):
        return self.intake.get("solar_design_graph_sha256")

    @property
    def graph_project_id(self):
        graph = self.graph
        return None if graph is None else graph["project"]["id"]


@dataclass(frozen=True)
class PreparedProjectGraphCommit:
    request_bytes: bytes
    output_intake_bytes: bytes
    receipt_bytes: bytes

    @property
    def request(self):
        return json.loads(self.request_bytes)

    @property
    def request_sha256(self):
        return _sha(self.request_bytes)

    @property
    def request_id(self):
        return uuid5(NAMESPACE_URL, SCHEMA + ":" + self.request_sha256)

    @property
    def output_intake_sha256(self):
        return _sha(self.output_intake_bytes)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _companion(intake):
    has_graph = "solar_design_graph" in intake
    has_digest = "solar_design_graph_sha256" in intake
    if has_graph != has_digest:
        raise GraphValidationError("INVALID_SEED_PARENT")
    if has_graph:
        graph = validate_graph(intake["solar_design_graph"])
        if intake["solar_design_graph_sha256"] != local.digest(graph):
            raise GraphValidationError("GRAPH_DIGEST_MISMATCH")
        return graph
    return None


def resolve_project_graph_context(verified_context):
    """Interpret exact verified content without conflating the two project IDs."""
    if not isinstance(verified_context, project.VerifiedProjectContext):
        _reject()
    binding = verified_context.binding
    version = binding.version
    identities = (binding.organization_id, binding.project_id, binding.drawing_id,
                  binding.input_version_id)
    if (any(not isinstance(value, UUID) for value in identities)
            or identities != (version.org_id, version.project_id, version.drawing_id,
                              version.version_id)
            or version.deleted_at is not None
            or verified_context.intake_ref != version.intake_ref):
        _reject()
    raw = verified_context.intake_bytes
    if not isinstance(raw, bytes) or _sha(raw) != verified_context.intake_sha256:
        project.refuse("INTAKE_DIGEST_MISMATCH")
    try:
        intake = json.loads(raw, object_pairs_hook=project._object_pairs,
                            parse_constant=project._nonfinite, parse_float=project._json_float)
        if type(intake) is not dict or local.canonical_bytes(intake) != local.canonical_bytes(verified_context.intake):
            _reject()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _reject()
    _companion(intake)
    return ProjectGraphContext(*identities, raw, verified_context.intake_sha256,
                               _utc(version.created_at))


def _canonical_source_intake(context):
    """Trusted source content from this exact canonical parent, never a head."""
    intake = context.intake
    if _companion(intake) is None:
        raise GraphValidationError("GRAPH_NOT_EMBEDDED")
    del intake["solar_design_graph"]
    del intake["solar_design_graph_sha256"]
    return intake


def _tool(tool, manifest):
    if not isinstance(tool, str) or tool not in SUPPORTED_TOOLS:
        raise project.ProjectContextError("SIP_R3_TOOL_UNSUPPORTED")
    declaration = solar_tools.get(tool)
    record = solar_tools.trusted_record(tool)
    if (declaration is None or record is None
            or deps.catalog_tool_digest(record) != manifest):
        raise project.ProjectContextError("SIP_R3_TOOL_MANIFEST_MISMATCH")
    return declaration


def _parameters(context, params):
    _bounded_json(params)
    if type(params) is not dict:
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    if not local.stable_numbers(params):
        raise GraphValidationError("INVALID_NUMERIC_PARAM")
    if "drawing_id" in params and params["drawing_id"] != str(context.drawing_id):
        raise GraphValidationError("DRAWING_ID_CONFLICT")
    normalized = copy.deepcopy(params)
    normalized.pop("drawing_id", None)
    return normalized


def _creation_id(context, tool, params, kind, index):
    """One derived entity ID: a pure function of the parent, the request and the slot."""
    raw = local.canonical_bytes({
        "schema": CREATION_SCHEMA,
        "organization_id": str(context.organization_id),
        "project_id": str(context.project_id),
        "drawing_id": str(context.drawing_id),
        "parent_version_id": str(context.parent_version_id),
        "parent_intake_sha256": context.intake_sha256,
        "created_at": context.created_at,
        "tool": tool,
        "parameters": params,
        "kind": kind,
        "index": index,
    })
    return "leaf:" + kind + ":" + str(UUID(bytes=hashlib.sha256(raw).digest()[:16], version=4))


def _creation(context, tool, params):
    """The private creation inputs a creating builtin consumes in place of UUIDs and the clock.

    `created_at` is the exact parent's stored timestamp. `new_id(kind, index, occupied)` returns
    `_creation_id` for that slot, or refuses DUPLICATE_APPLICATION_ID when the derived ID is
    already in `occupied` (every entity ID in the graph plus IDs allocated earlier in this
    candidate), so a derived ID never overwrites or adopts an existing entity. `params` is
    copied here, so nothing the builtin does later can move a derived ID. Holds no shared state.
    """
    frozen = copy.deepcopy(params)

    def new_id(kind, index, occupied):
        ref = _creation_id(context, tool, frozen, kind, index)
        if ref in occupied:
            raise GraphValidationError("DUPLICATE_APPLICATION_ID")
        return ref

    return {"created_at": context.created_at, "new_id": new_id}


def _run_builtin(context, tool, graph, params, trusted, job_id):
    """The canonical builtin dispatcher; it never reaches an outbound service."""
    if tool == SIZING_TOOL:
        if type(params) is not dict or params.get("mode") != MANUAL_SIZING_MODE:
            raise project.ProjectContextError("SIP_R3_SERVICE_EVIDENCE_REQUIRED")
        return local._load_builtin(tool).run_bound(
            graph, params, tenant_id=str(context.organization_id), job_id=str(job_id))
    if tool in CREATION_TOOLS:
        # Single-add needs its operation guard here; multi-add validates its own operation.
        if tool == STRING_TOOL and (type(params) is not dict
                                    or params.get("operation") != STRING_OPERATION):
            raise GraphValidationError("INVALID_STRING_ADD_REQUEST")
        return local._load_builtin(tool).run(graph, params, **trusted,
                                             _creation=_creation(context, tool, params))
    return local._load_builtin(tool).run(graph, params, **trusted)


def _candidate(context, tool, params, initialized, job_id, *, conn=None):
    before = _companion(context.intake)
    builtin_params = copy.deepcopy(params)
    base = None
    if initialized:
        initialize = validate_seed_request(builtin_params.pop("initialize", None))
        if before is not None:
            raise GraphValidationError("GRAPH_ALREADY_EMBEDDED")
        if initialize["source_intake_sha256"] != context.intake_sha256:
            raise GraphValidationError("SOURCE_HASH_MISMATCH")
        base = new_empty_graph(tenant_id=str(context.organization_id),
            drawing_id=str(context.drawing_id), source_hash=context.intake_sha256,
            units=initialize["units"], created_at=context.created_at)
    else:
        if before is None:
            raise GraphValidationError("GRAPH_NOT_EMBEDDED")
        if "initialize" in builtin_params:
            raise GraphValidationError("GRAPH_ALREADY_EMBEDDED")
    trusted = {}
    if "source_intake" in solar_tools.get(tool)["trusted_inputs"]:
        trusted["source_intake"] = _canonical_source_intake(context)
    if not initialized and tool not in ("solar-settings", "solar-panels-from-drawing", SIZING_TOOL):
        require_project_sizing(context, conn=conn)
    if _global_sizing(tool, params) and conn is not None:
        after = _stored_sizing_graph(context, job_id, conn)
    else:
        after = _run_builtin(context, tool, copy.deepcopy(base if initialized else before),
                             builtin_params, trusted, job_id)
    if builtin_params.get("cancel") is True:
        raise GraphValidationError("GRAPH_COMMIT_CANCELLED")
    output = local.canonical_bytes(local.version_companion(context.intake, before, after))
    return before, after, base, output


def _prepare(context, tool, params, *, checkout, job_id, attempt, tool_manifest_sha256,
             initialized, conn=None):
    if isinstance(context, project.VerifiedProjectContext):
        context = resolve_project_graph_context(context)
    if not isinstance(context, ProjectGraphContext):
        _reject()
    _tool(tool, tool_manifest_sha256)
    if (not isinstance(checkout, project.CheckoutLease)
            or (checkout.organization_id, checkout.project_id, checkout.drawing_id)
            != (context.organization_id, context.project_id, context.drawing_id)
            or not isinstance(checkout.holder_binding_id, UUID)
            or type(checkout.fence) is not int or not 1 <= checkout.fence <= 9223372036854775807
            or not isinstance(job_id, UUID) or type(attempt) is not int or attempt < 1):
        _reject()
    parameters = _parameters(context, params)
    before, after, base, output = _candidate(context, tool, parameters, initialized, job_id, conn=conn)
    request = {"schema": SCHEMA, "organization_id": str(context.organization_id),
        "project_id": str(context.project_id), "drawing_id": str(context.drawing_id),
        "parent_version_id": str(context.parent_version_id),
        "actor_binding_id": str(checkout.holder_binding_id), "checkout_fence": str(checkout.fence),
        "job_id": str(job_id), "attempt": attempt, "tool": tool,
        "tool_manifest_sha256": tool_manifest_sha256, "parameters": parameters,
        "parent_intake_sha256": context.intake_sha256,
        "before_graph_sha256": None if before is None else local.digest(before),
        "graph_project_id": after["project"]["id"], "initialized": initialized}
    if _global_sizing(tool, parameters):
        request["sizing_exchange"] = after["settings"]["extra"]["string_sizing"]["project_exchange"]
        project.platform_link._canonical_jobs_module()._validate_graph_request(
            _stored_job(job_id, conn), request)
    raw = local.canonical_bytes(request)
    request_sha256 = _sha(raw)
    receipt = {**request, "adapter": ADAPTER_KIND,
        "request_id": str(uuid5(NAMESPACE_URL, SCHEMA + ":" + request_sha256)),
        "request_sha256": request_sha256, "output_intake_sha256": _sha(output),
        "graph_sha256": local.digest(after), "before_rev": None if before is None else before["rev"],
        "after_rev": after["rev"], "seed_base_rev": 0 if initialized else None,
        "seed_base_graph_sha256": local.digest(base) if initialized else None,
        "drawing_changed": True}
    return PreparedProjectGraphCommit(raw, output, local.canonical_bytes(receipt))


def prepare_project_graph_seed(context, params, *, checkout, job_id, attempt, tool_manifest_sha256,
                               conn=None):
    return _prepare(context, "solar-settings", params, checkout=checkout, job_id=job_id,
                    attempt=attempt, tool_manifest_sha256=tool_manifest_sha256, initialized=True, conn=conn)


def prepare_project_graph_commit(context, tool, params, *, checkout, job_id, attempt, tool_manifest_sha256,
                                 conn=None):
    return _prepare(context, tool, params, checkout=checkout, job_id=job_id,
                    attempt=attempt, tool_manifest_sha256=tool_manifest_sha256, initialized=False, conn=conn)


def _stored_context(request, version_id, conn):
    binding = project.graph_store().resolve_version_binding(
        UUID(request["organization_id"]), UUID(request["project_id"]), version_id,
        drawing_id=UUID(request["drawing_id"]), conn=conn)
    return resolve_project_graph_context(project._read_intake(binding)), binding.version


def _global_sizing(tool, params):
    return tool == SIZING_TOOL and type(params) is dict and params.get("mode") == "global"


def _exchanges():
    from leaf_platform import project_sizing_store
    return project_sizing_store


def _stored_job(job_id, conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (job_id,))
        row = cur.fetchone()
    if row is None:
        _reject()
    return row


def _exchange_authority(row):
    jobs = project.platform_link._canonical_jobs_module()
    org, proj, parent, job_id, drawing, actor = jobs._graph_ids(row)
    if not _global_sizing(row["tool_name"], row["params"]):
        _reject()
    targets = row["params"].get("requests")
    if type(targets) is not dict or len(targets) != 1:
        _reject()
    worker = (row.get("lease_owner") if row["status"] == "running"
              else (row.get("provenance") or {}).get("worker_id"))
    return (_exchanges().ExchangeScope(org, proj, drawing, job_id, actor,
                next(iter(targets)), row["request_tenant_id"]),
            _exchanges().JobAttempt(worker, row["attempt"]))


def _sizing_expected(row, context, exchange):
    scope, _ = _exchange_authority(row)
    if ((scope.org_id, scope.project_id, scope.drawing_id, UUID(str(row["input_version_id"])))
            != (context.organization_id, context.project_id, context.drawing_id, context.parent_version_id)
            or row["execution_context"]["parent_intake_sha256"] != context.intake_sha256):
        _reject()
    return {"identity": {"organization_id": str(scope.org_id), "project_id": str(scope.project_id),
                "drawing_id": str(scope.drawing_id), "job_id": str(scope.job_id),
                "actor_id": str(scope.actor_binding_id), "target_settings_id": scope.target_settings_id},
            "parent_version_id": str(context.parent_version_id),
            "parent_intake_sha256": context.intake_sha256, "parameters": row["params"],
            "tool_manifest_sha256": row["execution_context"]["tool_manifest_sha256"],
            "producer": {"adapter_version": cloud.ADAPTER_VERSION, "service_identity": cloud.SIZING_URL,
                         "http_status": 200, "worker_id": exchange["sender_worker"],
                         "attempt": exchange["sender_attempt"]},
            "classification": exchange["classification"], "evidence_sha256": exchange["evidence_sha256"]}


def _stored_sizing_graph(context, job_id, conn):
    row = _stored_job(job_id, conn)
    scope, lease = _exchange_authority(row)
    exchange = _exchanges().get_exchange(scope, lease, conn=conn)
    if exchange is None or exchange["state"] not in ("response_stored", "published"):
        _reject()
    expected = _sizing_expected(row, context, exchange)
    if (exchange["request_sha256"] != _sha(exchange["request_bytes"])
            or exchange["response_sha256"] != _sha(exchange["response_bytes"])
            or exchange["evidence_sha256"] != _sha(exchange["evidence_bytes"])):
        _reject()
    proved = sizing.prove_sizing_evidence(json.loads(exchange["evidence_bytes"]),
        exchange["request_bytes"], exchange["response_bytes"], context.graph, expected=expected)
    after = proved["graph"]
    after["settings"]["extra"]["string_sizing"]["project_exchange"] = {
        "job_id": str(job_id), "evidence_sha256": exchange["evidence_sha256"]}
    return after


def _prove_exchange_state(request, result, conn, *, pending=False):
    if not _global_sizing(request["tool"], request["parameters"]):
        return
    row = _stored_job(UUID(request["job_id"]), conn)
    jobs = project.platform_link._canonical_jobs_module()
    jobs._validate_graph_request(row, request)
    jobs._graph_scope(row, conn)
    scope, lease = _exchange_authority(row)
    exchange = _exchanges().get_exchange(scope, lease, conn=conn)
    if exchange is None or request.get("sizing_exchange") != {
            "job_id": str(scope.job_id), "evidence_sha256": exchange["evidence_sha256"]}:
        _reject()
    if pending and row["status"] == "running" and exchange["state"] == "response_stored":
        _exchanges()._lock(conn, _exchanges()._parameters(scope, lease))
        return
    if (row["status"] != "succeeded" or exchange["state"] != "published"
            or str(row["output_version_id"]) != result["output_version_id"]
            or str(exchange["output_version_id"]) != result["output_version_id"]
            or exchange["published_worker"] != lease.worker_id
            or exchange["published_attempt"] != lease.attempt
            or (row.get("result") or {}).get("graph_commit") != result):
        _reject()


def require_project_sizing(context, *, conn=None):
    """Prove origin and current electrical values while permitting later graph revisions."""
    current = context.graph
    evidence = (current or {}).get("settings", {}).get("extra", {}).get("string_sizing")
    if not evidence:
        return
    if evidence.get("mode") == MANUAL_SIZING_MODE:
        # A manual label is only an exemption once its evidence is the exact manual record with
        # an unknown electrical boundary; anything else is refused like forged cloud evidence.
        try:
            cloud.require_sizing(current)
        except (ValueError, TypeError, KeyError, AttributeError, GraphValidationError):
            _reject()
        return
    ref = evidence.get("project_exchange")
    if type(ref) is not dict or set(ref) != {"job_id", "evidence_sha256"}:
        raise project.ProjectContextError("SIP_R3_SERVICE_EVIDENCE_REQUIRED")
    if conn is None:
        return project.platform_link.platform_db().run_transaction(
            lambda connection: require_project_sizing(context, conn=connection))
    try:
        row = _stored_job(UUID(ref["job_id"]), conn)
        scope, _ = _exchange_authority(row)
        if (scope.org_id, scope.project_id, scope.drawing_id) != (
                context.organization_id, context.project_id, context.drawing_id):
            _reject()
        parent, _ = _stored_context({"organization_id": str(scope.org_id),
            "project_id": str(scope.project_id), "drawing_id": str(scope.drawing_id)},
            UUID(str(row["input_version_id"])), conn)
        from solar_project_jobs import prepare_project_graph_job
        prepared = prepare_project_graph_job(row, conn=conn)
        project_graph_job_provenance(scope.job_id, row["result"]["graph_commit"], expected=prepared, conn=conn)
        original = json.loads(prepared.output_intake_bytes)["solar_design_graph"]
        cloud.require_sizing(current)
        old = original["settings"]
        if (evidence != old["extra"]["string_sizing"]
                or any(current["settings"][key] != old[key] for key in (
                    "panels_in_sequence", "voc_cold", "global_string_sizing_confirmed"))):
            _reject()
        from solar_sizing_power import frame_module_power
        for frame in current["frames"]:
            if frame["module_power_watts"] != frame_module_power(
                    current, frame["panel_refs"], frame["electrical_zone_ref"]):
                _reject()
    except (ValueError, TypeError, KeyError, AttributeError, GraphValidationError):
        _reject()


def _reprepare(context, request, conn=None):
    # No lease lookup: these are the original trusted publication identities.
    lease = project.CheckoutLease(context.organization_id, context.project_id, context.drawing_id,
        "Stored publication", UUID(request["actor_binding_id"]), None, None,
        int(request["checkout_fence"]))
    if type(request["initialized"]) is not bool:
        _reject()
    prepared = _prepare(context, request["tool"], request["parameters"], checkout=lease,
        job_id=UUID(request["job_id"]), attempt=request["attempt"],
        tool_manifest_sha256=request["tool_manifest_sha256"], initialized=request["initialized"], conn=conn)
    if prepared.request_bytes != local.canonical_bytes(request):
        _reject()
    return prepared


def _receipt(prepared, version):
    result = json.loads(prepared.receipt_bytes)
    result["output_version_id"] = str(version.version_id)
    return result


def _publication_fingerprint(version, conn):
    if version.provenance.get("schema") != "leaf.project-drawing-publication.v1":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT import_fingerprint FROM drawing_versions WHERE org_id=%s "
                    "AND project_id=%s AND version_id=%s AND deleted_at IS NULL",
                    (version.org_id, version.project_id, version.version_id))
        row = cur.fetchone()
        if (row is None or row["import_fingerprint"]
                != project.graph_store()._publication_fingerprint(version.provenance)):
            _reject()


def publish_project_graph_commit(prepared, *, actor_binding_id, conn):
    if not isinstance(prepared, PreparedProjectGraphCommit):
        _reject()
    request = prepared.request
    if not isinstance(actor_binding_id, UUID) or str(actor_binding_id) != request["actor_binding_id"]:
        _reject()
    org, proj, drawing, parent = (UUID(request[key]) for key in
        ("organization_id", "project_id", "drawing_id", "parent_version_id"))

    def publish_immutable_intake():
        try:
            context, _version = _stored_context(request, parent, conn)
            rebuilt = _reprepare(context, request, conn)
            if rebuilt != prepared:
                _reject()
        except project.ProjectContextError as exc:
            if exc.reason_code.startswith("SIP_R1_"):
                raise
            _reject()
        except (GraphValidationError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            _reject()
        key = project.graph_store().publication_intake_key(org, proj, drawing,
                                                         prepared.output_intake_sha256)
        project.write_loop.upload_backend_for_tenant(str(org)).put_if_absent_or_verify(
            key, prepared.output_intake_bytes)
        project._load_intake(org, key, prepared.output_intake_sha256)

    version = project.graph_store().publish_version(org, proj, drawing,
        expected_parent_version_id=parent, actor_binding_id=actor_binding_id,
        expected_fence=int(request["checkout_fence"]), request_id=prepared.request_id,
        intake_sha256=prepared.output_intake_sha256, conn=conn, before_new=publish_immutable_intake)
    return _receipt(prepared, version)


def project_graph_commit_provenance(tenant, result, *, expected, conn=None):
    """Verify historical stored bytes against a separately trusted request."""
    if conn is None:
        return project.platform_link.platform_db().run_transaction(
            lambda connection: project_graph_commit_provenance(tenant, result,
                                                               expected=expected, conn=connection))

    def authorize(request):
        if str(tenant) != request["organization_id"]:
            _reject()
        org, _actor = project._access(tenant, UUID(request["project_id"]), write=False)
        if str(org) != request["organization_id"]:
            _reject()
        return org

    return _project_graph_provenance(result, expected=expected, conn=conn, authorize=authorize)


def project_graph_job_provenance(job_id, result, *, expected, conn):
    return _job_provenance(job_id, result, expected=expected, conn=conn, pending=False)


def _pending_project_graph_job_provenance(job_id, result, *, expected, conn):
    return _job_provenance(job_id, result, expected=expected, conn=conn, pending=True)


def _job_provenance(job_id, result, *, expected, conn, pending):
    """Prove from a freshly loaded job's stored editor, never request identity."""
    jobs = project.platform_link._canonical_jobs_module()
    if not isinstance(job_id, UUID):
        jobs._graph_binding_error()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (job_id,))
        row = cur.fetchone()
    if row is None:
        project.refuse("CONTEXT_NOT_FOUND")
    request = expected.request if isinstance(expected, PreparedProjectGraphCommit) else expected
    jobs._validate_graph_request(row, request)
    jobs._graph_scope(row, conn)
    return _project_graph_provenance(result, expected=expected, conn=conn,
                                    authorize=lambda request: UUID(str(row["org_id"])), pending=pending)


def _project_graph_provenance(result, *, expected, conn, authorize, pending=False):
    """Shared historical byte reconstruction with authority supplied internally."""
    try:
        request = expected.request if isinstance(expected, PreparedProjectGraphCommit) else copy.deepcopy(expected)
        if type(request) is not dict or type(result) is not dict:
            _reject()
        request_sha256 = _sha(local.canonical_bytes(request))
        request_id = uuid5(NAMESPACE_URL, SCHEMA + ":" + request_sha256)
        if (result.get("request_sha256") != request_sha256
                or result.get("request_id") != str(request_id)
                or any(key not in result for key in request)
                or local.canonical_bytes({key: result[key] for key in request})
                != local.canonical_bytes(request)):
            _reject()
        org = authorize(request)
        _prove_exchange_state(request, result, conn, pending=pending)
        parent, parent_version = _stored_context(request, UUID(request["parent_version_id"]), conn)
        _publication_fingerprint(parent_version, conn)
        try:
            output, output_version = _stored_context(request, UUID(result["output_version_id"]), conn)
        except project.ProjectContextError as exc:
            if exc.reason_code == "SIP_R1_CONTEXT_NOT_FOUND":
                _reject()
            raise
        prepared = _reprepare(parent, request, conn)
        if (local.canonical_bytes(result) != local.canonical_bytes(_receipt(prepared, output_version))
                or output.intake_bytes != prepared.output_intake_bytes):
            _reject()
        graph_store = project.graph_store()
        proof = graph_store._publication_proof(org, parent.project_id, parent.drawing_id,
            parent.parent_version_id, UUID(request["actor_binding_id"]), int(request["checkout_fence"]),
            prepared.request_id, prepared.output_intake_sha256, parent_version.oss_object)
        if (output_version.provenance != proof
                or output_version.oss_object != parent_version.oss_object
                or output_version.idempotency_key != "sip-r2:" + str(prepared.request_id)):
            _reject()
        _publication_fingerprint(output_version, conn)
        # Independently rebuild the companion from both stored graphs as well.
        if local.canonical_bytes(local.version_companion(parent.intake, parent.graph, output.graph)) != output.intake_bytes:
            _reject()
        return {"adapter": ADAPTER_KIND, "request_id": str(prepared.request_id),
                "request_sha256": prepared.request_sha256,
                "output_version_id": str(output_version.version_id),
                "output_intake_sha256": prepared.output_intake_sha256,
                "graph_sha256": output.graph_sha256}
    except project.ProjectContextError as exc:
        if exc.reason_code.startswith("SIP_R1_"):
            raise
        _reject()
    except (GraphValidationError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _reject()


def run_project_graph_seed(tenant, context, params, *, checkout, job_id, attempt, tool_manifest_sha256):
    if isinstance(context, project.VerifiedProjectContext):
        context = resolve_project_graph_context(context)
    if not isinstance(context, ProjectGraphContext):
        _reject()
    org, actor = project._access(tenant, context.project_id, write=True)
    if (org != context.organization_id or not isinstance(checkout, project.CheckoutLease)
            or actor != checkout.holder_binding_id):
        _reject()
    prepared = prepare_project_graph_seed(context, params, checkout=checkout, job_id=job_id,
        attempt=attempt, tool_manifest_sha256=tool_manifest_sha256)
    return project._mutation(lambda conn: publish_project_graph_commit(prepared,
                                               actor_binding_id=actor, conn=conn))


def run_project_graph_commit(tenant, context, tool, params, *, checkout, job_id, attempt, tool_manifest_sha256):
    if isinstance(context, project.VerifiedProjectContext):
        context = resolve_project_graph_context(context)
    if not isinstance(context, ProjectGraphContext):
        _reject()
    org, actor = project._access(tenant, context.project_id, write=True)
    if (org != context.organization_id or not isinstance(checkout, project.CheckoutLease)
            or actor != checkout.holder_binding_id):
        _reject()
    prepared = prepare_project_graph_commit(context, tool, params, checkout=checkout, job_id=job_id,
        attempt=attempt, tool_manifest_sha256=tool_manifest_sha256)
    return project._mutation(lambda conn: publish_project_graph_commit(prepared,
                                               actor_binding_id=actor, conn=conn))
