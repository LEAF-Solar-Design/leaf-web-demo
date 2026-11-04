"""Canonical Ground Physical HTTP contracts; memory is not a fence race proof."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import checkout_capability as caps
import deps
import entitlements
import envelopes
import solar_civil_operations as civil
import solar_frames_piles as fp
import solar_ground_terrain_adapter as adapter
import solar_landxml_import as landxml
import solar_local_graph as local
import solar_physical_head as ph
import solar_physical_source as sources
import solar_physical_state as ps
import solar_project_artifacts as artifacts
import solar_project_context as project
import solar_project_graph as graph
import solar_project_physical as physical
import solar_project_physical_read as reads
from solar_design_graph import GraphValidationError
from routers import project_drawings, project_physical as routes, drawings, solar_terrain
from route_flatten import iter_leaf_routes
import test_sip_r6_read as read_cases
import test_sip_r6_artifacts as artifact_cases
import test_solar_landxml_import as xml_cases
import test_solar_frames_piles as frame_cases


@pytest.fixture
def http(monkeypatch):
    s = read_cases.setup(monkeypatch)
    artifact_cases.immutable(s, monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "b35be9230c184da187fcdf805bd863476165abc508e609b7f136bf6e60c246b3d")
    principal = SimpleNamespace(tenant=s.tenant, member=True, auth=200, access=0,
                                admission=0, produced=0, body_bytes=0)
    now = s.lease.acquired_at
    s.checkout = s.lease

    def access(tenant, proj, *, write):
        principal.access += 1
        if str(tenant) != str(s.org) or proj != s.project:
            project.refuse("CONTEXT_NOT_FOUND")
        if not principal.member:
            project.refuse("PROJECT_FORBIDDEN")
        return s.org, s.actor

    def checkout(org, proj, drawing, **kwargs):
        return project.CheckoutState(org, proj, drawing, s.checkout, s.lease.fence, now)

    def verify(org, proj, drawing, *, actor_binding_id, expected_fence, **kwargs):
        if s.checkout is None:
            project.refuse("CHECKOUT_REQUIRED")
        if s.checkout.expires_at <= now:
            project.refuse("CHECKOUT_EXPIRED")
        if expected_fence != s.checkout.fence:
            project.refuse("CHECKOUT_STALE")
        if actor_binding_id != s.checkout.holder_binding_id:
            project.refuse("CHECKOUT_DENIED")
        return s.checkout

    monkeypatch.setattr(project, "_access", access)
    monkeypatch.setattr(project.graph_store(), "get_checkout", checkout)
    monkeypatch.setattr(project.graph_store(), "verify_checkout", verify)
    monkeypatch.setattr(project, "_mutation", lambda operation: operation(s))
    admit = project.verify_at_admission
    def admission(*args, **kwargs):
        principal.admission += 1
        return admit(*args, **kwargs)
    monkeypatch.setattr(project, "verify_at_admission", admission)
    for name in ("run_operation", "run_landxml_import"):
        original = getattr(physical, name)
        def record(*args, _call=original, **kwargs):
            principal.produced += 1
            return _call(*args, **kwargs)
        monkeypatch.setattr(physical, name, record)
    stream = Request.stream
    async def counted(request):
        async for chunk in stream(request):
            principal.body_bytes += len(chunk)
            yield chunk
    monkeypatch.setattr(Request, "stream", counted)
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(project_drawings.no_store_responses)
    app.include_router(project_drawings.router)
    app.include_router(routes.router)
    def tenant():
        if principal.auth != 200:
            raise HTTPException(status_code=principal.auth)
        return principal.tenant
    app.dependency_overrides[deps.require_active_tenant] = tenant
    with TestClient(app) as client:
        yield SimpleNamespace(s=s, client=client, app=app, p=principal)


def path(h, version=None):
    s = h.s
    return f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{version or s.head}/physical"


def headers(h, **changes):
    s = h.s
    return {"X-Checkout-Capability": caps.mint(s.tenant,
        project.checkout_scope(s.project, s.drawing), s.lease.fence), **changes}


def operation(h, body=None, *, version=None, **kwargs):
    if body is None:
        head, _ = physical.load_bound(h.s.context(h.s.head))
        body = {"operation": "mesh", "expected_head": head["state"]["artifact_id"] if head else "0" * 64}
    return h.client.post(path(h, version) + "/operations", json=body,
                          headers=kwargs.pop("headers", headers(h)), **kwargs)


def import_xml(h, *, version=None, data=None, **kwargs):
    return h.client.post(path(h, version) + "/imports/landxml?drawing_units=m&crs=none&target_cells=2",
        content=xml_cases.landxml() if data is None else data,
        headers=kwargs.pop("headers", headers(h, **{"Content-Type": "application/xml"})), **kwargs)


def refusal(response, code, status):
    assert response.status_code == status, response.text
    assert response.json()["error"]["reason_code"] == code
    assert response.headers["cache-control"] == "no-store"


def success(response):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def all_paths(h):
    return (("GET", path(h)), ("POST", path(h) + "/operations"),
            ("POST", path(h) + "/imports/landxml"), ("POST", path(h) + "/reads"))


def test_routes_mounted_authenticated_and_flagged(http, monkeypatch):
    from app import app
    expected = {("GET", routes.PREFIX), *(('POST', routes.PREFIX + suffix)
        for suffix in ("/operations", "/imports/landxml", "/reads"))}
    counts = dict.fromkeys(expected, 0)
    for route_path, route in iter_leaf_routes(app.routes):
        for method in getattr(route, "methods", ()) or ():
            pair = method, route_path
            if pair in counts:
                counts[pair] += 1
                assert deps.require_active_tenant in [d.call for d in route.dependant.dependencies]
    assert set(counts.values()) == {1}
    for value in (None, "0", "true", " 1", "01", ""):
        if value is None:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        for method, url in all_paths(http):
            refusal(http.client.request(method, url, content=b"secret"), "project_execution_disabled", 409)
    assert (http.p.access, http.p.admission, http.p.produced, http.p.body_bytes) == (0, 0, 0, 0)
    refusal(http.client.get(path(http).replace(str(http.s.project), "bad")), "SIP_R6_INVALID_REQUEST", 400)


def test_view_unbound_version_is_empty_not_409(http):
    for view in ("terrain", "civil"):
        body = success(http.client.get(path(http) + "?view=" + view))
        assert body["stored"] is False and body["head"] is None
        if view == "terrain":
            assert body["terrain"] is None
        else:
            assert body["preview"] is body["standing"] is None and body["grade_pads"] == 0
    refusal(http.client.get(path(http) + "?view=other"), "SIP_R6_INVALID_REQUEST", 400)


def test_view_terrain_and_civil_match_pure_helpers(http):
    success(import_xml(http))
    head, document = physical.load_bound(http.s.context(http.s.head))
    assert set(head["state"]) == set(artifacts._REF_KEYS) | {"schema", "download"}
    assert len(head["state"]) == 13 and "source_version" not in head["state"]
    assert head["project_id"] != head["state"]["project_id"]
    for view, result in (("terrain", routes._terrain_result(head, document)),
                         ("civil", civil.civil_view(head, document))):
        body = success(http.client.get(path(http) + "?view=" + view))
        assert body["result"] == result
        assert not {"error", "degraded_mode"} & set(result)
        for key in set(result) - {"schema"}:
            assert body[key] == result[key]


def test_view_follows_exact_version_and_graph_commit_carries_it(http):
    s = http.s
    first = s.head
    a = success(import_xml(http))
    second = s.head
    b = success(import_xml(http, data=xml_cases.landxml(points=["0 0 20", "0 10 21", "10 0 22", "10 10 23"])))
    third = s.head
    assert success(http.client.get(path(http, first)))["stored"] is False
    old = success(http.client.get(path(http, second)))
    assert old["head"] == a["result"]["head"] and old["context"]["is_head"] is False
    assert success(http.client.get(path(http, third)))["head"] == b["result"]["head"]
    ctx = graph.resolve_project_graph_context(s.context(third))
    read_cases.tools_cases.commit(s, "solar-settings", {"expected_rev": ctx.graph["rev"],
        "project_changes": {"name": "Physical carried forward"}}, ctx)
    assert s.head != third
    assert success(http.client.get(path(http)))["head"] == b["result"]["head"]


def test_cross_project_isolation(http, monkeypatch):
    s = http.s
    before = deepcopy(s.blobs), len(s.versions)
    for method, url in all_paths(http):
        media = "application/xml" if url.endswith("landxml") else "application/json"
        for foreign in (str(s.project), str(s.drawing), str(s.head)):
            refusal(http.client.request(method, url.replace(foreign, str(uuid4())),
                content=b"secret", headers=headers(http, **{"Content-Type": media})),
                "SIP_R1_CONTEXT_NOT_FOUND", 404)
        refusal(http.client.request(method, url, content=b"secret", headers=headers(http,
            **{"X-Org-Id": str(uuid4()), "Content-Type": "application/json"})), "SIP_R1_CONTEXT_NOT_FOUND", 404)
        http.p.member = False
        refusal(http.client.request(method, url, content=b"secret", headers=headers(http,
            **{"Content-Type": "application/xml" if url.endswith("landxml") else "application/json"})),
            "SIP_R1_PROJECT_FORBIDDEN", 403)
        http.p.member = True
        with monkeypatch.context() as patch:
            def demo(*args):
                raise project.ProjectContextError("demo_actor_refused")
            patch.setattr(project_drawings, "_headers", demo)
            refusal(http.client.request(method, url, content=b"secret"), "demo_actor_refused", 403)
    assert (s.blobs, len(s.versions)) == before
    assert http.p.produced == http.p.body_bytes == 0


def test_entitlements_kept_and_passed_through(http, monkeypatch):
    for unavailable in (False, True):
        with monkeypatch.context() as patch:
            def policy(*args):
                if unavailable:
                    raise entitlements.EntitlementsError("policy")
                return {}
            patch.setattr(entitlements, "entitlements_for", policy)
            for method, url in all_paths(http):
                permission = "run_read" if method == "GET" or url.endswith("reads") else (
                    "upload" if url.endswith("landxml") else "run_write")
                legacy = (entitlements.policy_unavailable_response if unavailable else
                          entitlements.entitlement_denied_response)(permission, entitlements.resolve_tier(http.s.tenant))
                response = http.client.request(method, url, content=b"secret")
                assert response.status_code == legacy.status_code and response.content == legacy.body
                assert response.headers["cache-control"] == "no-store"
    assert http.p.access == http.p.body_bytes == http.p.produced == 0


def test_write_fence_at_admission(http):
    s = http.s
    for call in (operation, import_xml):
        refusal(call(http, headers={"Content-Type": "application/xml" if call is import_xml else "application/json"}),
                "SIP_R1_CHECKOUT_DENIED", 403)
        s.checkout = None
        refusal(call(http), "SIP_R1_CHECKOUT_REQUIRED", 409)
        s.checkout = replace(s.lease, expires_at=s.lease.acquired_at - timedelta(seconds=1))
        refusal(call(http), "SIP_R1_CHECKOUT_EXPIRED", 409)
        s.checkout = s.lease
        wrong = caps.mint(s.tenant, project.checkout_scope(s.project, uuid4()), s.lease.fence)
        refusal(call(http, headers={"X-Checkout-Capability": wrong,
            "Content-Type": "application/xml" if call is import_xml else "application/json"}),
            "SIP_R1_CHECKOUT_DENIED", 403)
        s.checkout = replace(s.lease, fence=s.lease.fence + 1)
        refusal(call(http), "SIP_R1_CHECKOUT_DENIED", 403)
        s.checkout = s.lease
        older = next(v.version_id for v in s.versions.values() if v.version_id != s.head)
        refusal(call(http, version=older), "SIP_R1_STALE_VERSION", 409)
    assert http.p.body_bytes == http.p.produced == 0


def test_write_check_order(http):
    for suffix, limit, media, large in (("/operations", civil.MAX_CIVIL_BODY_BYTES, "TERRAIN_MEDIA_TYPE_REFUSED", "TERRAIN_BODY_TOO_LARGE"),
            ("/imports/landxml", landxml.MAX_LANDXML_BYTES, "LANDXML_MEDIA_TYPE_REFUSED", "LANDXML_TOO_LARGE")):
        for header, code, status in (({"Content-Type": "text/plain"}, media, 415),
                ({"Content-Type": "application/xml" if "landxml" in suffix else "application/json",
                  "Content-Length": str(limit + 1)}, large, 413)):
            before = http.p.access, http.p.admission, http.p.body_bytes
            refusal(http.client.post(path(http) + suffix, content=b"x", headers=header), code, status)
            assert (http.p.access, http.p.admission, http.p.body_bytes) == before
        before = http.p.admission
        response = http.client.post(path(http) + suffix, content=iter([b"x" * (limit + 1)]),
            headers=headers(http, **{"Content-Type": "application/xml" if "landxml" in suffix else "application/json"}))
        refusal(response, large, 413)
        assert http.p.admission == before + 1 and http.p.produced == 0
    for data, code in ((b'{"operation":"mesh","operation":"slope"}', "TERRAIN_BODY_INVALID"),
            (b'{"operation":"mesh","extra":1}', "TERRAIN_BODY_INVALID"),
            (b'{"operation":"mesh","limits":{}}', "TERRAIN_BODY_INVALID"),
            (b'{"operation":"other"}', "TERRAIN_OPERATION_INVALID"),
            (b'{"operation":"mesh","expected_head":"bad"}', "TERRAIN_EXPECTED_HEAD_INVALID")):
        refusal(http.client.post(path(http) + "/operations", content=data,
            headers=headers(http, **{"Content-Type": "application/json"})), code, 400)
    before = http.p.access
    refusal(http.client.post(path(http) + "/imports/landxml?target_cells=bad"), "LANDXML_TARGET_CELLS_INVALID", 400)
    assert http.p.access == before and http.p.produced == 0


def test_refusal_mapping(http, monkeypatch):
    for attribute, refusals, call in (("run_operation", routes.VIEW_REFUSALS, operation),
            ("run_landxml_import", drawings.LANDXML_IMPORT_REFUSALS, import_xml)):
        with monkeypatch.context() as patch:
            for code, (status, _error, _retry) in refusals.items():
                def fail(*args, _code=code, **kwargs):
                    raise ps.PhysicalStateError(_code)
                patch.setattr(physical, attribute, fail)
                refusal(call(http), code, status)
            for exc in (ps.PhysicalStateError("unknown-secret"), project.ProjectContextError("secret"), RuntimeError("secret")):
                def fail(*args, _exc=exc, **kwargs):
                    raise _exc
                patch.setattr(physical, attribute, fail)
                response = call(http)
                refusal(response, "PROJECT_PHYSICAL_INTERNAL", 500)
                assert "secret" not in response.text
    refusal(import_xml(http, data=b"<broken>"), "LANDXML_MALFORMED", 400)
    def bad(*args):
        raise fp.FramesPilesError("FRAMES_PILES_STATE_INVALID")
    monkeypatch.setattr(civil, "civil_view", bad)
    refusal(http.client.get(path(http) + "?view=civil"), "FRAMES_PILES_STATE_INVALID", 422)


def test_physical_reads(http, monkeypatch):
    success(import_xml(http))
    head = success(http.client.get(path(http)))["head"]
    def read(tool, params=None, **changes):
        return http.client.post(path(http) + "/reads", json={"tool": tool, "params": params or {},
            "catalog_digest": read_cases.tools_cases.manifest(tool), **changes})
    for tool in reads.SUPPORTED_PHYSICAL_READ_TOOLS:
        with monkeypatch.context() as patch:
            module = reads.local._load_builtin(tool)
            if tool == "solar-physical-shade":
                # Its CPU kernel has separate parity coverage; assert the adapter's exact input.
                def shade(g, params, *, physical_head):
                    assert physical_head["head"] == head
                    return {"head": physical_head["head"], "stored": True}
                patch.setattr(module, "run", shade)
            params = {"expected_head": head["state"]["artifact_id"], "format": "terrain-csv"} if tool.endswith("export") else {}
            result = success(read(tool, params))["result"]
            assert result["physical_state"] == head
            if tool.endswith("export"):
                ref = result["output"]["artifact"]
                assert http.client.get(ref["download"]).status_code == 200
    refusal(read(read_cases.CALC), "SIP_R6_TOOL_UNSUPPORTED", 409)
    refusal(read("solar-terrain-read", catalog_digest="sha256:" + "0" * 64), "SIP_R6_TOOL_MANIFEST_MISMATCH", 409)
    first = next(v.version_id for v in http.s.versions.values() if v.version_id != http.s.head)
    response = http.client.post(path(http, first) + "/reads", json={"tool": "solar-terrain-read",
        "catalog_digest": read_cases.tools_cases.manifest("solar-terrain-read"), "current": True})
    refusal(response, "SIP_R6_STALE_CURRENT", 409)
    before = list(http.s.writes)
    original_load = physical.load_bound
    count = 0
    def swapped(verified):
        nonlocal count
        count += 1
        bound, document = original_load(verified)
        if count == 2:
            bound["state"]["artifact_id"] = "f" * 64
        return bound, document
    with monkeypatch.context() as patch:
        patch.setattr(physical, "load_bound", swapped)
        refusal(read("solar-terrain-read"), "SIP_R6_ARTIFACT_SOURCE_MISMATCH", 500)
    with monkeypatch.context() as patch:
        patch.setattr(reads.local._load_builtin("solar-terrain-read"), "run", lambda *a, **k:
            {"head": head, "large": "x" * reads.MAX_OUTPUT_BYTES})
        refusal(read("solar-terrain-read"), "READ_OUTPUT_LIMIT_EXCEEDED", 500)
    assert http.s.writes == before


def test_no_store_on_every_new_path(http):
    for status in (401, 422):
        http.p.auth = status
        for method, url in all_paths(http):
            response = http.client.request(method, url)
            assert response.status_code == status and response.headers["cache-control"] == "no-store"


def test_canonical_chain_on_memory(http):
    s = http.s
    initial = s.head
    imported = success(import_xml(http))
    first = s.head
    ref = physical.check_ref(s.context(first).intake[physical.REF_KEY])
    assert ref["index"] == 0 and ref["parent"] is None
    generated = success(operation(http, {"operation": fp.GENERATE, "expected_head": ref["artifact_id"],
        "boundary": frame_cases.SQUARE_M, "preset": frame_cases.PRESET, "drawing_units": "m"}))
    second = s.head
    ref2 = physical.check_ref(s.context(second).intake[physical.REF_KEY])
    assert ref2["index"] == 1 and ref2["parent"] == ref["artifact_id"]
    assert success(http.client.get(path(http, first)))["head"] == imported["result"]["head"]
    assert success(http.client.get(path(http, second)))["head"] == generated["result"]["head"]
    refusal(import_xml(http, version=initial), "SIP_R1_STALE_VERSION", 409)
    success(operation(http))
    before = len(s.versions), list(s.writes)
    unchanged = success(operation(http))
    assert unchanged["context"]["created"] is False
    assert unchanged["context"]["output_version_id"] == str(s.head)
    assert (len(s.versions), s.writes) == before


def _replace_intake(s, intake):
    version = s.versions[s.head]
    raw = local.canonical_bytes(intake)
    digest = sha256(raw).hexdigest()
    key = project.graph_store().publication_intake_key(s.org, s.project, s.drawing, digest)
    version.intake_ref = key
    version.provenance["intake"] = {"ref": key, "sha256": digest}
    s.blobs[key] = raw


def test_generation_cap(http):
    success(import_xml(http))
    intake = http.s.context(http.s.head).intake
    intake[physical.REF_KEY]["index"] = 4095
    _replace_intake(http.s, intake)
    before = deepcopy(http.s.blobs), len(http.s.versions), list(http.s.writes)
    refusal(operation(http), "PHYSICAL_HEAD_LOG_FULL", 409)
    assert (http.s.blobs, len(http.s.versions), http.s.writes) == before


def test_corrupt_or_missing_bound_state_fails_closed(http):
    success(import_xml(http))
    s = http.s
    original = deepcopy(s.blobs), deepcopy(s.versions)
    for corruption in ("extra", "uppercase", "missing", "digest", "tool"):
        s.blobs, s.versions = deepcopy(original)
        intake = s.context(s.head).intake
        ref = intake[physical.REF_KEY]
        meta_key = artifacts._key({"organization_id": str(s.org), "project_id": str(s.project),
            "drawing_id": str(s.drawing)}, ref["artifact_id"])
        if corruption == "extra":
            ref["extra"] = True
            _replace_intake(s, intake)
        elif corruption == "uppercase":
            ref["artifact_id"] = ref["artifact_id"].upper()
            _replace_intake(s, intake)
        elif corruption == "missing":
            del s.blobs[artifacts._key(json.loads(s.blobs[meta_key]), ref["content_sha256"], blob=True)]
        elif corruption == "digest":
            s.blobs[artifacts._key(json.loads(s.blobs[meta_key]), ref["content_sha256"], blob=True)] = b"{}"
        else:
            meta = json.loads(s.blobs[meta_key])
            meta["tool"] = "other"
            s.blobs[meta_key] = local.canonical_bytes(meta)
        before = deepcopy(s.blobs), len(s.versions), list(s.writes)
        refusal(http.client.get(path(http)), "PHYSICAL_HEAD_CORRUPT", 500)
        # Keep the request valid even when the stored ref contains uppercase
        # hex, so admission reaches the bound-state reader rather than the
        # request parser's expected-head check.
        refusal(operation(http, {"operation": "mesh", "expected_head": ref["artifact_id"].lower()}), "PHYSICAL_HEAD_CORRUPT", 500)
        assert (s.blobs, len(s.versions), s.writes) == before


def test_legacy_source_is_default(monkeypatch, tmp_path):
    from test_w1_design_graph import graph as graph_fixture
    from test_w1_solve_commit import seed
    from test_solar_physical_state import TENANT, DRAWING, PROJECT
    backend, _ = seed(tmp_path, monkeypatch, graph_fixture.__wrapped__())
    import write_loop
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    landxml.import_landxml_terrain(backend, TENANT, DRAWING, xml_cases.landxml(), drawing_units="m", crs="none")
    source = sources.LegacySource(backend, TENANT, DRAWING, PROJECT)
    assert source.project_id() == PROJECT
    for enabled in ("0", "1"):
        monkeypatch.setenv("LEAF_SOLAR_PHYSICAL_VERSIONED", enabled)
        assert source.parent()[0] == ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    # Every producer must construct the same legacy seam by default. Stop at
    # project resolution to compare call order without modifying its state.
    calls = []
    class Probe(sources.LegacySource):
        def project_id(self):
            calls.append("project")
            raise GraphValidationError("GRAPH_NOT_EMBEDDED")
    for module in (adapter, fp, civil, landxml):
        monkeypatch.setattr(module, "LegacySource", Probe)
    common = (backend, TENANT, DRAWING)
    invocations = (
        (adapter.render_mesh, {"expected_head": "0" * 64}),
        (adapter.check_tracker_slope, {"expected_head": "0" * 64}),
        (adapter.clear_tracker_slope, {"expected_head": "0" * 64}),
        (fp.generate_frames, {"base": None, "boundary": frame_cases.SQUARE_M, "preset": frame_cases.PRESET, "drawing_units": "m"}),
        (fp.detect_collisions, {"base": None}),
        (fp.generate_piles, {"base": None, "preset": frame_cases.PRESET, "pile_template": frame_cases.TEMPLATE}),
        (fp.check_pile_lengths, {"base": None, "preset": frame_cases.PRESET}),
        (civil.grade_pad, {"base": None, "boundary": frame_cases.SQUARE_M}),
        (civil.operate, {"body": {"operation": fp.COLLISION, "expected_head": None}}),
        (landxml.import_landxml_terrain, {"data": xml_cases.landxml(), "drawing_units": "m", "crs": "none"}),
    )
    for call, kwargs in invocations:
        for explicit in (False, True):
            before = len(calls)
            with pytest.raises(ps.PhysicalStateError):
                call(*common, **kwargs, **({"source": Probe(*common, None)} if explicit else {}))
            assert calls[before:] == ["project"]
