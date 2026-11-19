"""Selected project versions must reach the declared warm read artifact."""
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID

import instant_execution as instant
import solar_project_context
from test_instant_session_wiring import _assignment, _Response
from test_project_execution_context import ORG, PROJECT, DRAWING, VERSION, OTHER, BOUND


def test_selected_version_layer_inventory(monkeypatch):
    from leaf_platform import entitlements
    monkeypatch.setattr(entitlements, "stored_job_entitlement_denial", lambda *_: None)
    instant._reset_for_tests()
    session = _assignment()["session_id"]
    intake = {"layers": ["0", "Unused", "Nested"], "inserts": [{"layer": "Modules"}],
              "blockdefs": {"Frame": [{"layer": "Nested"}]}}
    posts = []
    monkeypatch.setattr(instant, "_control_url", lambda: "http://localhost")
    monkeypatch.setattr(instant, "_control_headers", lambda: {"x-instant-control-secret": "fixture"})
    monkeypatch.setattr(instant.customization_service, "effective_catalog_pin",
                        lambda _: {"catalog_commit": "1" * 40, "effective_catalog_digest": "2" * 64})

    def stage(url, *, json, **kwargs):
        posts.append((url, json))
        return _Response(dict(_assignment(ORG, session), drawing_context=json["drawing_context"]["reference"],
                              code_digest=json["artifact"]["code_digest"], artifact_digest=json["artifact"]["artifact_digest"],
                              effective_catalog_digest=json["effective_catalog_digest"]))

    def resolve(tenant, project_id, version, *, drawing_id, write):
        assert tenant == ORG and str(project_id) == PROJECT and str(drawing_id) == DRAWING and write is False
        return SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(ORG), project_id=project_id,
                               drawing_id=drawing_id, input_version_id=version), intake=intake,
                               intake_sha256=sha256(json.dumps(intake).encode()).hexdigest())

    monkeypatch.setattr(instant.requests, "post", stage)
    monkeypatch.setattr(solar_project_context, "resolve_context", resolve)
    for version, label in [(VERSION, "Unused"), (OTHER, "Renamed")]:
        intake["layers"][1] = label
        bound = dict(BOUND, input_version_id=version, intake_sha256=sha256(json.dumps(intake).encode()).hexdigest())
        assignment = instant.project_assignment(ORG, session, bound)
        assert assignment["drawing_context"]["version_id"] == version
        body = posts[-1][1]
        source = body["artifact"]["source"]
        assert "sha256:" + sha256(source.encode()).hexdigest() == assignment["code_digest"]
        # Only execute the checked-in fixture artifact verified by the platform registry.
        from instant_tools.list_layers import run
        from pathlib import Path
        assert Path(run.__code__.co_filename).read_text(encoding="utf-8") == source
        result, _ = run({"drawing_context": body["drawing_context"]["data"]}, {})
        assert result == {"layers": sorted(["0", "Modules", "Nested", label]), "count": 4}
        nested, _ = run({"drawing_context": body["drawing_context"]["data"]}, {"prefix": "Nest"})
        assert nested == {"layers": ["Nested"], "count": 1}
        if version == OTHER:
            assert "Unused" not in result["layers"] and "Renamed" in result["layers"]
        assert instant.project_assignment(ORG, session, bound) == assignment
    assert len(posts) == 2 and posts[1][0].endswith("/rebind")
    assert instant.project_assignment(ORG, session, dict(bound, intake_sha256="0"*64)) is None
    assert len(posts) == 2
    monkeypatch.setattr(entitlements, "stored_job_entitlement_denial", lambda *_: object())
    assert instant.project_assignment(ORG, session, bound) is None
    assert len(posts) == 2
