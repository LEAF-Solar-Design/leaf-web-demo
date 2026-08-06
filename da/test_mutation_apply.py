"""Offline tests for LeafApplyMutations script and protected provisioning."""
from __future__ import annotations

import json

import pytest

import apply_lisp
import mutation_apply as subject


class Response:
    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class Http:
    def __init__(self, post=(), patch=(), get=(), delete=()):
        self.responses = {
            "post": list(post), "patch": list(patch), "get": list(get),
            "delete": list(delete),
        }
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses[method].pop(0)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._call("patch", url, **kwargs)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("delete", url, **kwargs)


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    monkeypatch.setattr(
        subject.client, "_auth_headers",
        lambda: {"Authorization": "Bearer secret"},
    )


def test_fixed_script_is_crlf_closed_format_and_never_evaluates_plan():
    script = apply_lisp.build_apply_scr()
    assert script.endswith("\r\n")
    assert "REMOVE" in script and "TRANSFORM" in script and "ADD" in script
    assert "LEAF_MUTATION_PLAN|1" in script
    assert "BASE_SHA256" in script
    assert "AcDbPolyline" in script and "(cons 210 normal)" in script
    assert "(entmod out)" in script and "(entupd e)" in script
    assert '"mutation-plan.txt"' in script
    assert '"output.dwg"' in script
    lowered = script.lower()
    assert "(eval " not in lowered
    assert "(read " not in lowered
    assert "(load " not in lowered
    assert "vl-load-com" not in lowered


def test_fixed_script_pins_add_layer_and_remove_mvp_constraints():
    script = apply_lisp.build_apply_scr()
    assert 'ok (>= (length raw) 3)' in script
    assert '0123456789_.-$ ")))' in script
    assert '(= kind "LWPOLYLINE")' in script
    assert '(member kind' not in script
    assert '"POLYLINE"' not in script


def test_activity_spec_pins_pure_script_contract():
    spec = subject.activity_spec()
    assert spec["id"] == "LeafApplyMutations"
    assert spec["engine"] == "Autodesk.AutoCAD+26_0"
    assert "appbundles" not in spec
    assert spec["commandLine"] == [
        subject.COMMAND_LINE, subject.INSPECTION_COMMAND_LINE,
    ]
    assert "$(args[Result].path)" in subject.INSPECTION_COMMAND_LINE
    assert "$(args[HostDwg].path)" not in subject.INSPECTION_COMMAND_LINE
    assert spec["parameters"] == {
        "HostDwg": {"verb": "get", "required": True, "localName": "host.dwg"},
        "Plan": {"verb": "get", "required": True, "localName": "mutation-plan.txt"},
        "Result": {"verb": "put", "required": True, "localName": "output.dwg"},
        "Intake": {
            "verb": "put", "required": False,
            "localName": "output-intake.txt",
        },
    }
    assert spec["settings"]["script"]["value"] == apply_lisp.build_apply_scr()
    assert '"output-intake.txt"' in spec["settings"]["inspectScript"]["value"]


def test_409_advances_version_and_patches_alias(monkeypatch):
    http = Http(
        post=[Response(409), Response(201, {"version": 4}), Response(409)],
        patch=[Response(200)],
    )
    monkeypatch.setattr(subject, "requests", http)
    result = subject.provision_activity()
    assert result == {
        "id": "LeafApplyMutations", "alias": "prod", "version": 4,
        "advanced": True,
    }
    body = json.loads(http.calls[1][2]["data"])
    assert "id" not in body
    assert http.calls[1][1].endswith("/activities/LeafApplyMutations/versions")
    assert http.calls[-1][1].endswith("/activities/LeafApplyMutations/aliases/prod")
    assert json.loads(http.calls[-1][2]["data"]) == {"version": 4}


def test_create_points_new_alias_without_patch(monkeypatch):
    http = Http(post=[Response(201, {"version": 1}), Response(201)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.provision_activity()
    assert result["version"] == 1 and result["advanced"] is False
    assert not http.responses["patch"]


def test_readiness_resolves_alias_version_and_matches(monkeypatch):
    http = Http(get=[
        Response(200, {"id": "prod", "version": 7}),
        Response(200, subject.activity_spec()),
    ])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    assert result == {
        "ready": True, "mismatches": [],
        "activity": {"alias": "prod", "version": 7},
    }
    assert http.calls[1][1].endswith("/activities/LeafApplyMutations/versions/7")


def test_alias_state_captures_version_or_absence(monkeypatch):
    http = Http(get=[Response(200, {"version": 7}), Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.alias_state() == {
        "id": "LeafApplyMutations", "alias": "prod", "exists": True,
        "version": 7,
    }
    assert subject.alias_state() == {
        "id": "LeafApplyMutations", "alias": "prod", "exists": False,
        "version": None,
    }


def test_restore_alias_repoints_or_removes_exact_alias(monkeypatch):
    http = Http(
        patch=[Response(200)], delete=[Response(204)],
        get=[Response(200, {"version": 3}), Response(404)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.restore_alias(3)["version"] == 3
    assert json.loads(http.calls[0][2]["data"]) == {"version": 3}
    assert subject.restore_alias(None)["exists"] is False
    assert [call[0] for call in http.calls] == ["patch", "get", "delete", "get"]


def test_restore_alias_recreates_missing_alias_and_reads_it_back(monkeypatch):
    http = Http(
        patch=[Response(404)], post=[Response(201)],
        get=[Response(200, {"version": 5})],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.restore_alias(5)["version"] == 5
    assert [call[0] for call in http.calls] == ["patch", "post", "get"]


def test_restore_alias_fails_on_wrong_readback(monkeypatch):
    http = Http(patch=[Response(200)], get=[Response(200, {"version": 6})])
    monkeypatch.setattr(subject, "requests", http)
    with pytest.raises(RuntimeError, match="readback mismatch"):
        subject.restore_alias(5)


@pytest.mark.parametrize("mutation,expected", [
    (lambda spec: spec.update({"engine": "wrong"}), "activity engine mismatch"),
    (lambda spec: spec.update({"commandLine": ["unsafe"]}), "activity commandLine mismatch"),
    (lambda spec: spec["parameters"].pop("Plan"), "activity parameters mismatch"),
    (lambda spec: spec["settings"]["script"].update({"value": "unsafe"}), "activity settings mismatch"),
    (lambda spec: spec.update({"appbundles": ["unexpected"]}), "activity appbundles mismatch"),
])
def test_readiness_mismatch_fails_closed(monkeypatch, mutation, expected):
    spec = subject.activity_spec()
    mutation(spec)
    http = Http(get=[Response(200, {"version": 2}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    assert result["ready"] is False
    assert expected in result["mismatches"]


def test_readiness_http_failure_fails_closed_without_secret(monkeypatch, capsys):
    secret = "SUPER-SECRET-TOKEN"
    http = Http(get=[Response(500, text=secret)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    combined = json.dumps(result) + capsys.readouterr().out + capsys.readouterr().err
    assert result["ready"] is False
    assert secret not in combined
    assert "Bearer secret" not in combined
    assert "HTTP 500" in result["mismatches"][0]


def test_module_has_no_paid_workitem_or_import_time_provisioning_surface():
    assert not hasattr(subject, "submit_workitem")
    assert not hasattr(subject, "provision_all")


def test_cli_provision_emits_stable_nonsecret_json(monkeypatch, capsys):
    monkeypatch.setattr(subject, "provision_activity", lambda: {
        "id": "LeafApplyMutations", "alias": "prod", "version": 8,
        "advanced": True,
    })
    assert subject.main(["provision", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "operation": "provision", "id": "LeafApplyMutations",
        "alias": "prod", "version": 8, "advanced": True,
    }


def test_cli_readiness_mismatch_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(subject, "readiness", lambda: {
        "ready": False, "mismatches": ["activity engine mismatch"],
    })
    assert subject.main(["readiness", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "readiness", "ready": False,
        "mismatches": ["activity engine mismatch"],
    }


@pytest.mark.parametrize("raw,expected", [("9", 9), ("absent", None)])
def test_cli_restore_alias_parses_and_receipts(monkeypatch, capsys, raw, expected):
    observed = []

    def restore(version):
        observed.append(version)
        return {
            "id": "LeafApplyMutations", "alias": "prod",
            "exists": version is not None, "version": version,
        }

    monkeypatch.setattr(subject, "restore_alias", restore)
    assert subject.main(["restore-alias", "--version", raw, "--json"]) == 0
    assert observed == [expected]
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True and receipt["version"] == expected


def test_cli_failure_redacts_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        subject, "provision_activity",
        lambda: (_ for _ in ()).throw(RuntimeError("secret signed form")),
    )
    assert subject.main(["provision", "--json"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["error"] == "provision failed"
