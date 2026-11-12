"""Private, confirmation-scoped checkout delegation. Never stores a capability."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4
import time
import json

from psycopg.types.json import Jsonb
import agent_gate
import customization_service
import deps
import platform_link
import project_execution_context as execution
import session_store
import solar_project_context as project
import solar_project_graph as graph
import turn_runner

PINS = ("catalog_digest", "catalog_commit", "effective_catalog_digest", "tool_manifest_sha256")
SOLVE_TOOL = "solar-solve-proposal"
_SEAL = object()


class CustodyError(ValueError):
    def __init__(self, code="checkout_custody_denied", status_code=403):
        if code not in ("checkout_custody_denied", "checkout_custody_unavailable"):
            code, status_code = "checkout_custody_denied", 403
        super().__init__(code)
        self.code, self.status_code = code, status_code


def _deny():
    raise CustodyError()


def _unavailable():
    raise CustodyError("checkout_custody_unavailable", 503)


def _protect(operation):
    try:
        return operation()
    except CustodyError:
        raise
    except (execution.ContextError, project.ProjectContextError, platform_link.ProjectSessionForbidden):
        _deny()
    except Exception:
        _unavailable()


def _subject(tenant, *, direct=False):
    subject = getattr(tenant, "subject", None)
    if (not isinstance(tenant, deps.TenantContext) or not isinstance(subject, str) or not subject
            or not getattr(tenant, "authority_resolved", False)
            or (direct and getattr(tenant, "backedge", False))):
        _deny()
    return subject


def _time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            _deny()
        return value.timestamp()
    if type(value) in (int, float):
        import math
        if math.isfinite(value):
            return float(value)
    if isinstance(value, str):
        return _time(datetime.fromisoformat(value.replace("Z", "+00:00")))
    _deny()


def _same(left, right):
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        _deny()


def _approval(tenant, confirmation_id, session_id, *, consumed=None):
    subject = _subject(tenant)
    approval = session_store.get_approval(confirmation_id)
    if (not approval or approval.get("tenant_id") != str(tenant)
            or approval.get("session_id") != session_id or approval.get("decided") is not True
            or approval.get("approved") is not True or approval.get("decided_by") != subject
            or approval.get("kind") != "run_capability"
            or _time(approval.get("expires_at")) <= time.time()
            or (consumed is not None and approval.get("consumed") is not consumed)):
        _deny()
    solve = approval.get("tool") == SOLVE_TOOL
    if approval.get("capability") != ("solve" if solve else "drawing.write"):
        _deny()
    session = session_store.get_session(session_id)
    if platform_link.require_project_session_access(session, tenant, write=True) is None:
        _deny()
    binding = execution.stored_binding(approval.get("payload"))
    if binding is None:
        _deny()
    binding = execution.revalidate(tenant, session, binding)
    org, actor = project._access(tenant, UUID(binding["project_id"]), write=True)
    if str(org) != binding["organization_id"]:
        _deny()
    record, status = agent_gate.read_pending_strict(confirmation_id)
    if status == "io_error":
        _unavailable()
    if (status != "ok" or not record or record.get("tenant_id") != str(tenant)
            or record.get("granted") is not True or record.get("denied") is not False
            or record.get("decided_by") != subject
            or record.get("action") != ("run_solve_tool" if solve else "run_write_tool")
            or _time(record.get("expires_at")) <= time.time()
            or (consumed is True and "consumed_at" not in record)):
        _deny()
    args = deepcopy(record.get("args"))
    if (not isinstance(args, dict) or record.get("args_hash") != agent_gate.canonical_args_hash(args)
            or args.get("tool") != approval.get("tool") or not _same(args.get("params"), approval.get("params"))
            or args.get("dwg") != (approval.get("payload") or {}).get("dwg")
            or args.get("dwg") != binding["drawing_id"]
            or args.get(execution.KEY) != binding):
        _deny()
    args.pop("confirmation_id", None)
    tool = deps.find_tool(args["tool"], str(tenant))
    if not tool or args["tool"] not in graph.SUPPORTED_TOOLS or not all(args.get(k) for k in PINS):
        _deny()
    trusted = __import__("solar_tools").trusted_record(args["tool"])
    if (trusted is None or deps.catalog_tool_digest(tool) != deps.catalog_tool_digest(trusted)
            or args["tool_manifest_sha256"] != deps.catalog_tool_digest(trusted)
            or args["catalog_digest"] != deps.catalog_tool_view(tool)["catalog_digest"]):
        _deny()
    pin = customization_service.effective_catalog_pin(str(tenant)) or deps.base_catalog_pin(deps.all_tools(str(tenant)))
    if any(args[k] != pin[k] for k in ("catalog_commit", "effective_catalog_digest")):
        _deny()
    if solve:
        solve_context = args.get("solve_context")
        if (type(solve_context) is not dict
                or set(solve_context) != {"frame_ref", "expected_rev", "phase"}
                or not isinstance(solve_context["frame_ref"], str) or not solve_context["frame_ref"]
                or type(solve_context["expected_rev"]) is not int or solve_context["expected_rev"] < 0
                or solve_context["phase"] not in ("initial", "background")):
            _deny()
        verified = project.resolve_context(tenant, UUID(binding["project_id"]),
            UUID(binding["input_version_id"]), drawing_id=UUID(binding["drawing_id"]), write=True)
        selected = graph.resolve_project_graph_context(verified).graph
        if (verified.intake_sha256 != binding["intake_sha256"] or not isinstance(selected, dict)
                or type(selected.get("rev")) is not int
                or selected["rev"] != solve_context["expected_rev"]):
            _deny()
    return approval, binding, args, str(actor), min(_time(approval["expires_at"]), _time(record["expires_at"]))


def _select(conn, tenant, confirmation_id, *, lock=False):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM project_checkout_custody WHERE tenant_id=%s AND confirmation_id=%s" +
                    (" FOR UPDATE" if lock else ""), (str(tenant), confirmation_id))
        return cur.fetchone()


def _valid(row, tenant, session_id, binding, args, actor):
    if (not row or row["revoked_at"] is not None or _time(row["expires_at"]) <= time.time()
            or row["tenant_id"] != str(tenant) or row["subject"] != _subject(tenant)
            or row["session_id"] != session_id or str(row["actor_binding_id"]) != actor
            or not _same(row["input_binding"], binding) or not _same(row["approved_args"], args)):
        _deny()


def issue(tenant, confirmation_id, session_id, input_version_id, capability):
    def operation():
        subject = _subject(tenant, direct=True)
        approval, binding, args, actor, expires = _approval(tenant, confirmation_id, session_id)
        if input_version_id != binding["input_version_id"]:
            _deny()
        admission = project.verify_at_admission(tenant, UUID(binding["project_id"]),
            UUID(binding["drawing_id"]), UUID(binding["input_version_id"]), checkout_capability=capability)
        if admission.context.intake_sha256 != binding["intake_sha256"]:
            _deny()
        lease = admission.checkout
        expires = datetime.fromtimestamp(min(expires, _time(lease.expires_at)), timezone.utc)
        def save(conn):
            row = _select(conn, tenant, confirmation_id, lock=True)
            if row is None:
                if approval.get("consumed") is not False:
                    _deny()
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO project_checkout_custody
                        (delegation_id,tenant_id,confirmation_id,session_id,subject,actor_binding_id,
                         input_binding,approved_args,fence,expires_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (tenant_id,confirmation_id) DO NOTHING""",
                        (uuid4(), str(tenant), confirmation_id, session_id, subject, UUID(actor),
                         Jsonb(binding), Jsonb(args), lease.fence, expires))
                row = _select(conn, tenant, confirmation_id, lock=True)
            _valid(row, tenant, session_id, binding, args, actor)
            if row["fence"] != lease.fence:
                _deny()
            return {"delegated": True, "delegation_id": str(row["delegation_id"]),
                    "fence": row["fence"], "expires_at": row["expires_at"].isoformat()}
        return platform_link.platform_db().run_transaction(save)
    return _protect(operation)


def revoke(tenant, confirmation_id, session_id, delegation_id):
    def operation():
        subject = _subject(tenant, direct=True)
        try:
            delegation_id_value = UUID(delegation_id)
        except (ValueError, TypeError, AttributeError):
            _deny()
        def remove(conn):
            with conn.cursor() as cur:
                cur.execute("""UPDATE project_checkout_custody SET revoked_at=COALESCE(revoked_at,NOW())
                    WHERE tenant_id=%s AND confirmation_id=%s AND session_id=%s
                      AND delegation_id=%s AND subject=%s RETURNING delegation_id""",
                    (str(tenant), confirmation_id, session_id, delegation_id_value, subject))
                return {"revoked": cur.fetchone() is not None}
        return platform_link.platform_db().run_transaction(remove)
    return _protect(operation)


def _extras(value):
    if value is None:
        return {}
    if type(value) is not dict or set(value) - {"solve_context", "proposal_job_id"}:
        _deny()
    return {key: deepcopy(item) for key, item in value.items() if item is not None}


def _resolve(tenant, session_id, turn_id, tool, params, catalog_pins, input_binding, run_extras):
    binding = execution.resolve_turn_binding(session_id, turn_id, tenant)
    data = session_store.turn_started_data(session_id, turn_id, str(tenant))
    confirm = data.get("confirm") if isinstance(data, dict) else None
    if (binding != input_binding or not isinstance(confirm, dict) or confirm.get("approved") is not True
            or session_store.active_turn_subject(session_id, turn_id, str(tenant), turn_runner.turn_max_s()) != _subject(tenant)):
        _deny()
    cid = confirm.get("confirmation_id")
    approval, binding, args, actor, _expires = _approval(tenant, cid, session_id, consumed=True)
    proposal = {"tool": approval["tool"], "params": approval["params"],
                "capability": approval["capability"], "dwg": args["dwg"]}
    if (binding != input_binding or not _same(confirm.get("proposal"), proposal) or tool != args["tool"] or not _same(params, args["params"])
            or catalog_pins != {k: args[k] for k in PINS}
            or not _same(_extras(run_extras), _extras({k: args.get(k) for k in ("solve_context", "proposal_job_id")}))):
        _deny()
    row = platform_link.platform_db().run_transaction(lambda conn: _select(conn, tenant, cid))
    _valid(row, tenant, session_id, binding, args, actor)
    return row


def resolve_for_turn(tenant, session_id, turn_id, *, tool, params, catalog_pins, input_binding, run_extras=None):
    def operation():
        values = (session_id, turn_id, tool, deepcopy(params), deepcopy(catalog_pins), deepcopy(input_binding), _extras(run_extras))
        row = _resolve(tenant, *values)
        return DelegatedCheckout(_SEAL, row, values)
    return _protect(operation)


class DelegatedCheckout:
    """Server-only proof. Rechecking it never mints, acquires or renews a lease."""
    __slots__ = ("_identity", "_values", "_confirmation_id")

    def __init__(self, seal, row, values):
        if seal is not _SEAL:
            _deny()
        self._identity = str(row["delegation_id"])
        self._values = values
        self._confirmation_id = row["confirmation_id"]

    @property
    def idempotency_key(self):
        return "checkout-custody:" + self._identity

    @property
    def confirmation_id(self):
        return self._confirmation_id

    @property
    def input_binding(self):
        return deepcopy(self._values[5])

    def fence_for(self, tenant, scope, state):
        try:
            row = _protect(lambda: _resolve(tenant, *self._values))
            binding = row["input_binding"]
            lease = state.checkout
            if (str(row["delegation_id"]) != self._identity
                    or scope != project.checkout_scope(binding["project_id"], binding["drawing_id"])
                    or str(state.organization_id) != binding["organization_id"]
                    or str(state.project_id) != binding["project_id"]
                    or str(state.drawing_id) != binding["drawing_id"]
                    or lease is None or lease.expires_at <= state.observed_at
                    or lease.fence != row["fence"] or str(lease.holder_binding_id) != str(row["actor_binding_id"])):
                _deny()
            return row["fence"]
        except CustodyError as exc:
            project.refuse("CHECKOUT_UNAVAILABLE" if exc.status_code == 503 else "CHECKOUT_DENIED")
