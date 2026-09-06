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


def test_fixed_script_pins_add_layer_and_remove_constraints():
    script = apply_lisp.build_apply_scr()
    assert 'ok (>= (length raw) 3)' in script
    assert '0123456789_.-$ ")))' in script
    # W4g-3 (contract v2): REMOVE, RELAYER and the SET* ops name the four
    # kinds the browser engine writes, as one closed list, and nothing else.
    assert script.count('(member kind (list "LWPOLYLINE" "LINE" "CIRCLE" "ARC"))') == 2
    assert '(member kind (list "LWPOLYLINE" "LINE"))' in script
    assert '"POLYLINE"' not in script
    assert '"INSERT"' not in script


def test_v2_script_accepts_both_plan_headers_and_carries_every_v2_line():
    script = apply_lisp.build_apply_scr()
    assert '(member line (list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2"))' in script
    for tag in ("ADDOPEN", "ADDLINE", "ADDCIRCLE", "ADDARC", "RELAYER",
                "SETPOINTS", "SETCIRCLE", "SETARC"):
        assert f'((= (car v) "{tag}")' in script, tag
        assert f'((= (car op) "{tag}")' in script, tag
    # Angles on the plan are degrees; entmake takes radians.
    assert "(defun leaf-deg2rad (d) (* pi (/ d 180.0)))" in script
    assert "(cons 50 (leaf-deg2rad" in script and "(cons 51 (leaf-deg2rad" in script
    # A LINE's endpoints come back to world coordinates through AutoCAD's
    # own trans from the plan's normal; nothing is evaluated.
    assert "(trans (list (car (car pts)) (cadr (car pts)) elev) normal 0)" in script
    lowered = script.lower()
    assert "(eval " not in lowered and "(read " not in lowered and "(load " not in lowered
    assert "vl-load-com" not in lowered
    for line in script.split("\r\n"):
        assert line.count("(") == line.count(")"), line[:80]


def test_mutation_inspect_script_is_the_extract_script_plus_the_three_kinds():
    from lisp import MUTATION_INSPECT_BLOCKS, build_scr
    plain = build_scr("output-intake.txt")
    extended = build_scr("output-intake.txt", extra_blocks=MUTATION_INSPECT_BLOCKS)
    assert build_scr() == build_scr(extra_blocks=())  # LeafExtract: byte-identical
    assert extended != plain
    head, tail = extended.split('(command "_.QUIT" "_Y")')
    assert head.startswith(plain.split('(command "_.QUIT" "_Y")')[0])
    for tag in ("LN|", "CI|", "AR|"):
        assert tag in extended and tag not in plain
    assert '"output-intake.txt"' in extended and "{OUT}" not in extended
    spec = subject.activity_spec()
    assert spec["settings"]["inspectScript"]["value"] == extended


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
        "contract": 2,
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


def test_readiness_accepts_aps_omission_of_optional_required_false(monkeypatch):
    spec = subject.activity_spec()
    spec["parameters"]["Intake"].pop("required")
    http = Http(get=[Response(200, {"version": 7}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)

    assert subject.readiness() == {
        "ready": True, "mismatches": [],
        "activity": {"alias": "prod", "version": 7},
        "contract": 2,
    }


def test_readiness_rejects_aps_omission_of_required_true(monkeypatch):
    spec = subject.activity_spec()
    spec["parameters"]["Result"].pop("required")
    http = Http(get=[Response(200, {"version": 7}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)

    result = subject.readiness()
    assert result["ready"] is False
    assert "activity parameters mismatch" in result["mismatches"]


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
    monkeypatch.setattr(subject, "provision_activity", lambda contract=2: {
        "id": "LeafApplyMutations", "alias": "prod", "version": 8,
        "advanced": True,
    })
    assert subject.main(["provision", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "operation": "provision", "id": "LeafApplyMutations",
        "alias": "prod", "version": 8, "advanced": True,
        "contract": 2,
    }


def test_cli_readiness_mismatch_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(subject, "readiness", lambda contract=2: {
        "ready": False, "mismatches": ["activity engine mismatch"],
    })
    assert subject.main(["readiness", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "readiness", "ready": False,
        "mismatches": ["activity engine mismatch"],
        "contract": 2,
    }


@pytest.mark.parametrize("raw,expected", [("9", 9), ("absent", None)])
def test_cli_restore_alias_parses_and_receipts(monkeypatch, capsys, raw, expected):
    observed = []

    def restore(version, contract=2):
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
        lambda contract=2: (_ for _ in ()).throw(RuntimeError("secret signed form")),
    )
    assert subject.main(["provision", "--json"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["error"] == "provision failed"


def test_v3_activity_extends_only_the_header_and_preserves_v2_settings():
    from lisp import MUTATION_INSPECT_BLOCKS, build_scr

    v2_settings = {
        "script": {"value": apply_lisp.build_apply_scr()},
        "inspectScript": {"value": build_scr(
            "output-intake.txt", extra_blocks=MUTATION_INSPECT_BLOCKS)},
    }
    v2 = subject.activity_spec()
    v3 = subject.activity_spec(3)
    assert v2["settings"] == v2_settings
    assert subject.activity_spec(2) == v2
    assert subject.ACTIVITY_ID == "LeafApplyMutations" and subject.ALIAS == "prod"
    assert v3["id"] == "LeafApplyMutationsV3"
    headers_v2 = '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2")'
    headers_v3 = '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2" "LEAF_MUTATION_PLAN|3")'
    script_v2 = v2_settings["script"]["value"]
    script_v3 = v3["settings"]["script"]["value"]
    assert script_v2.count(headers_v2) == 1
    assert "LEAF_MUTATION_PLAN|3" not in script_v2
    assert script_v3.count(headers_v3) == 1
    assert script_v3 == script_v2.replace(headers_v2, headers_v3)
    for version in (1, 2, 3):
        assert script_v3.count(f"LEAF_MUTATION_PLAN|{version}") == 1
    # No v3 operation is introduced by accepting the new header.
    assert next(line for line in script_v3.splitlines() if line.startswith("(defun leaf-parse-line")) == next(
        line for line in script_v2.splitlines() if line.startswith("(defun leaf-parse-line"))
    v3["id"] = v2["id"]
    v3["settings"]["script"]["value"] = script_v2
    assert v3 == v2
    assert subject.MUTATION_INSPECT_BLOCKS_V3 == MUTATION_INSPECT_BLOCKS


def test_v3_readiness_absent_alias_is_a_refusal(monkeypatch):
    http = Http(get=[Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness(3)
    assert result == {
        "ready": False, "contract": 3,
        "mismatches": ["activity alias read failed with HTTP 404"],
    }
    assert [call[1] for call in http.calls] == [
        f"{subject.client.DA}/activities/LeafApplyMutationsV3/aliases/prod",
    ]


def test_v3_readiness_compares_its_own_version(monkeypatch):
    http = Http(get=[Response(200, {"version": 9}), Response(200, subject.activity_spec(3))])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.readiness(3) == {
        "ready": True, "contract": 3, "mismatches": [],
        "activity": {"alias": "prod", "version": 9},
    }
    assert all("/activities/LeafApplyMutationsV3/" in call[1] for call in http.calls)


def test_v3_provision_advances_only_the_separate_activity(monkeypatch):
    http = Http(
        post=[Response(409), Response(201, {"version": 2}), Response(409)],
        patch=[Response(200)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.provision_activity(3) == {
        "id": "LeafApplyMutationsV3", "alias": "prod", "version": 2, "advanced": True,
    }
    assert json.loads(http.calls[0][2]["data"]) == subject.activity_spec(3)
    assert http.calls[1][1].endswith("/activities/LeafApplyMutationsV3/versions")
    assert http.calls[2][1].endswith("/activities/LeafApplyMutationsV3/aliases")
    assert http.calls[3][1].endswith("/activities/LeafApplyMutationsV3/aliases/prod")


def test_v3_alias_snapshot_and_restore_stay_on_the_separate_activity(monkeypatch):
    http = Http(
        get=[Response(200, {"version": 4}), Response(200, {"version": 3}), Response(404)],
        patch=[Response(404)], post=[Response(201)], delete=[Response(204)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.alias_state(3)["version"] == 4
    assert subject.restore_alias(3, contract=3)["version"] == 3
    assert subject.restore_alias(None, contract=3)["exists"] is False
    assert all("/activities/LeafApplyMutationsV3/" in call[1] for call in http.calls)
    assert [call[0] for call in http.calls] == ["get", "patch", "post", "get", "delete", "get"]


def test_cli_v3_readiness_echoes_contract_when_alias_is_absent(monkeypatch, capsys):
    http = Http(get=[Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.main(["readiness", "--contract", "3", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "readiness", "contract": 3, "ready": False,
        "mismatches": ["activity alias read failed with HTTP 404"],
    }


@pytest.mark.parametrize("command,function,extra", [
    ("provision", "provision_activity", []),
    ("readiness", "readiness", []),
    ("alias-state", "alias_state", []),
    ("restore-alias", "restore_alias", ["--version", "absent"]),
])
def test_every_cli_command_routes_the_contract(monkeypatch, capsys, command, function, extra):
    calls = []

    def run(*args, contract=2):
        calls.append((args, contract))
        return {"ready": True}

    monkeypatch.setattr(subject, function, run)
    assert subject.main([command, *extra, "--contract", "3", "--json"]) == 0
    assert calls == [((None,) if command == "restore-alias" else (), 3)]
    assert json.loads(capsys.readouterr().out)["contract"] == 3


@pytest.mark.parametrize("rgb,expected", [("12,34,56", [12, 34, 56]), ("~", None)])
def test_entity_properties_are_additive_after_a_line(rgb, expected):
    import intake_parse

    line = "LN|0|1.000,2.000,0.000|3.000,4.000,0.000|1A\n"
    legacy = intake_parse.parse_text(line, "test.dwg")
    parsed = intake_parse.parse_text(line + f"EP|1A|256|{rgb}|ByLayer|-1", "test.dwg")
    assert parsed.pop("properties") == {
        "1A": {"aci": 256, "rgb": expected, "linetype": "ByLayer", "lineweight": -1},
    }
    assert parsed == legacy
    assert len(parsed["polylines"]) == 1 and not parsed.get("parseErrors")


def test_dimension_record_reads_coordinate_and_angular_precisions():
    import intake_parse

    parsed = intake_parse.parse_text(
        "DM|linear|1.23456,2.34567,3.45678|4.56789,5.67891,6.78912|"
        "7.89123,8.91234,9.12345|30.12345678|Standard|0.0000004,0.0000006,1|12.34567|2A",
        "test.dwg",
    )
    assert parsed["dimensions"] == [{
        "type": "linear", "p1": [1.235, 2.346, 3.457], "p2": [4.568, 5.679, 6.789],
        "dimline": [7.891, 8.912, 9.123], "rotation_deg": 30.123457, "style": "Standard",
        "nrm": [0.0, 0.000001, 1.0], "measurement": 12.346, "handle": "2A",
    }]
    assert not parsed["polylines"] and not parsed.get("parseErrors")


@pytest.mark.parametrize("record,field", [
    ("EP|1A|7|~|Continuous|25", "properties"),
    ("DM|aligned|0,0,0|3,4,0|0,5,0|0|Standard|0,0,1|5|2A", "dimensions"),
])
def test_sidecar_records_close_a_complete_polyline_first(record, field):
    import intake_parse

    legacy = "PL|Panels|1|2|0,0,1|1A\nPV|0,0\nPV|2,0\nPV|2,2\nPX|Leaf\nPXS|kept\n"
    expected = intake_parse.parse_text(legacy, "test.dwg")
    parsed = intake_parse.parse_text(legacy + record + "\nPV|99,99\nPXS|ignored", "test.dwg")
    assert parsed.pop(field)
    assert parsed == expected
    assert len(parsed["polylines"]) == 1
    assert parsed["polylines"][0]["pts"] == [[0.0, 0.0, 2.0], [2.0, 0.0, 2.0], [2.0, 2.0, 2.0]]


@pytest.mark.parametrize("record", [
    "EP|1A|7|~|Continuous",
    "EP|1A|bad|~|Continuous|25",
    "EP|1A|7|1,2|Continuous|25",
    "EP|1A|7|1,2,256|Continuous|25",
    "EP|1A|7|~|Continuous|bad",
])
def test_malformed_properties_are_parse_errors(record):
    import intake_parse

    parsed = intake_parse.parse_text(record, "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("EP:")
    assert "properties" not in parsed
