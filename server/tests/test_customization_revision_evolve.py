from copy import deepcopy
import json

import pytest

import customization_service
from customization_models import ChangeSet, ChangeState
from customization_service import CustomizationService, CustomizationServiceError


CASES = [
    ("01-patch", None),
    ("02-unchanged-minor", "invalid_staged_catalog_revision"),
    ("03-optional-input", None),
    ("04-output-field", None),
    ("05-both", None),
    ("06-additive-patch", "invalid_staged_catalog_revision"),
    ("07-required-input", "invalid_staged_catalog_revision"),
    ("08-remove-input", "invalid_staged_catalog_revision"),
    ("09-retype-input", "invalid_staged_catalog_revision"),
    ("10-remove-output", "invalid_staged_catalog_revision"),
    ("11-retype-output", "invalid_staged_catalog_revision"),
    ("12-nested-change", "invalid_staged_catalog_revision"),
    ("13-root-keyword", "invalid_staged_catalog_revision"),
    ("14-major", "invalid_staged_catalog_revision"),
    ("15-minor-skip", "invalid_staged_catalog_revision"),
    ("16-nonzero-patch", "invalid_staged_catalog_revision"),
    ("17-malformed-version", "invalid_staged_catalog_revision"),
    ("18-legacy-missing", None),
    ("19-implicit-properties", None),
    ("20-capability", "invalid_staged_catalog_revision"),
    ("21-extra-path", "invalid_staged_paths"),
    ("22-body-mismatch", "invalid_staged_tool"),
    ("23-second-tool", "invalid_staged_catalog_delta"),
    ("24-required-removal", "invalid_staged_catalog_revision"),
    ("25-malformed-properties", "invalid_staged_catalog_revision"),
    ("26-empty-properties-only", "invalid_staged_catalog_revision"),
    ("27-non-object-root", "invalid_staged_catalog_revision"),
    ("28-base-properties-not-object", "invalid_staged_catalog_revision"),
    ("29-required-missing-property", "invalid_staged_catalog_revision"),
    ("30-duplicate-required", "invalid_staged_catalog_revision"),
    ("31-scalar-new-property", "invalid_staged_catalog_revision"),
    ("32-true-vs-one-property", "invalid_staged_catalog_revision"),
    ("33-true-vs-one-root", "invalid_staged_catalog_revision"),
    ("34-boolean-schema-true-to-one", "invalid_staged_catalog_revision"),
    ("35-patch-true-vs-one", "invalid_staged_catalog_revision"),
    ("36-int-vs-float", "invalid_staged_catalog_revision"),
    ("37-key-order-additive", None),
    ("38-deeper-than-bound", "invalid_staged_catalog_revision"),
]


@pytest.fixture
def base_tool():
    return {
        "name": "measure",
        "entry": "tools/measure/tool.py",
        "kind": "script",
        "engine_op": "measure",
        "capabilities": ["drawing.read"],
        "version": "1.2.3",
        "params": {
            "type": "object", "properties": {"x": {"type": "number"}},
            "required": ["x"],
        },
        "returns": {
            "type": "object", "properties": {"count": {"type": "number"}},
        },
    }


@pytest.fixture
def revision_change():
    return ChangeSet(
        change_set_id="test-change", tenant_id="test-tenant",
        idempotency_key="revise", state=ChangeState.STAGED, version=2,
        base_commit="base", staged_commit="staged", catalog_digest=None,
        desired_platform_release="test-release", workspace_contract_digest="",
        author_subject="test-author", approver_subject=None,
        created_at="", updated_at="", change_kind="revise",
        target_tool_name="measure",
    )


@pytest.fixture
def policy_repository(monkeypatch):
    bare = object()
    policy = object()
    monkeypatch.setattr(customization_service, "_bare_repo", lambda tenant: bare)
    monkeypatch.setattr(customization_service, "load_policy", lambda: policy)

    def install(base_tools, staged_tools, changed):
        blobs = {
            "base:registry.json": json.dumps({"tools": base_tools}).encode("utf-8"),
            "staged:registry.json": json.dumps({"tools": staged_tools}).encode("utf-8"),
        }

        def git(repo, *args):
            assert repo is bare
            if args[0] == "diff-tree":
                assert args == (
                    "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                    "base", "staged",
                )
                return "\0".join(changed) + "\0"
            assert args == ("ls-tree", "-r", "-z", "staged")
            return "".join(f"100644 blob {'0' * 40}\t{path}\0" for path in changed)

        def blob(repo, ref):
            assert repo is bare
            return blobs[ref]

        def classify(selected_policy, release, path):
            assert selected_policy is policy
            assert release == "test-release"
            assert path in changed
            return "frozen" if path == "registry.json" else "tenant_owned"

        monkeypatch.setattr(customization_service, "_git", git)
        monkeypatch.setattr(customization_service, "_git_blob", blob)
        monkeypatch.setattr(customization_service, "classify_path", classify)

    return install


@pytest.mark.parametrize(
    "case,error_code", CASES, ids=[f"W22D2-{case}" for case, _ in CASES],
)
def test_w22d2_revision_policy(
    case, error_code, base_tool, revision_change, policy_repository,
):
    number = int(case.split("-", 1)[0])
    staged = deepcopy(base_tool)
    staged["version"] = "1.3.0"
    changed = ["registry.json", "tools/measure/tool.py", "tools/measure/tool.json"]
    # Every refusal row except the version and path rows carries one valid
    # addition, so the rule the row names is the only thing that refuses it.
    if number in {3, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 17, 20, 21, 22, 23,
                  24, 27, 29, 30, 32, 33, 34, 36, 37, 38}:
        staged["params"]["properties"]["label"] = {"type": "string"}
    if number in {4, 5, 10, 11}:
        staged["returns"]["properties"]["unit"] = {"type": "string"}
    if number in {1, 6, 18, 35}:
        staged["version"] = "1.2.4"
    if number == 7:
        staged["params"]["required"].append("label")
    elif number == 8:
        del staged["params"]["properties"]["x"]
    elif number == 9:
        staged["params"]["properties"]["x"]["type"] = "string"
    elif number == 10:
        del staged["returns"]["properties"]["count"]
    elif number == 11:
        staged["returns"]["properties"]["count"]["type"] = "string"
    elif number == 12:
        staged["params"]["properties"]["x"]["minimum"] = 0
    elif number == 13:
        staged["params"]["additionalProperties"] = False
    elif number in {14, 15, 16, 17}:
        staged["version"] = {
            14: "2.3.0", 15: "1.4.0", 16: "1.3.1", 17: "1.3.0-beta",
        }[number]
    elif number == 18:
        for tool in (base_tool, staged):
            tool.pop("params")
            tool.pop("returns")
    elif number in {19, 26}:
        base_tool["returns"] = {"type": "object"}
        staged["returns"] = {
            "type": "object",
            "properties": {"unit": {"type": "string"}} if number == 19 else {},
        }
    elif number == 20:
        staged["capabilities"].append("drawing.write")
    elif number == 21:
        changed.append("tools/other/tool.py")
    elif number == 24:
        staged["params"]["required"] = []
    elif number == 25:
        # A list that names the existing property and an addition: without the
        # staged-properties dictionary guard the policy would index the list by
        # name and fail with a TypeError instead of this 422.
        staged["params"]["properties"] = ["x", "label"]
    elif number == 27:
        for tool in (base_tool, staged):
            tool["params"]["type"] = "array"
    elif number == 28:
        base_tool["params"] = {"type": "object", "properties": []}
        staged["params"] = {"type": "object", "properties": {"label": {"type": "string"}}}
    elif number in {29, 30}:
        for tool in (base_tool, staged):
            tool["params"]["required"] = ["missing"] if number == 29 else ["x", "x"]
    elif number == 31:
        staged["params"]["properties"]["label"] = "string"
    elif number in {32, 35}:
        base_tool["params"]["properties"]["x"] = {"enum": [True]}
        staged["params"]["properties"]["x"] = {"enum": [1]}
    elif number == 33:
        base_tool["params"]["enum"] = [{"x": True}]
        staged["params"]["enum"] = [{"x": 1}]
    elif number == 34:
        base_tool["params"]["properties"]["x"] = True
        staged["params"]["properties"]["x"] = 1
    elif number in {36, 37}:
        base_tool["params"]["properties"]["x"] = {"type": "number", "minimum": 0}
        staged["params"]["properties"]["x"] = (
            {"type": "number", "minimum": 0.0} if number == 36
            else {"minimum": 0, "type": "number"}
        )
    elif number == 38:
        nested = {"type": "number"}
        for _ in range(customization_service._SCHEMA_IDENTITY_MAX_DEPTH + 72):
            nested = {"not": nested}
        base_tool["params"]["properties"]["x"] = nested
        staged["params"]["properties"]["x"] = deepcopy(nested)
    body_tool = deepcopy(staged)
    if number == 22:
        body_tool["description"] = "Changed body"
    base_tools = [base_tool]
    staged_tools = [staged]
    if number == 23:
        other = deepcopy(base_tool)
        other.update(name="other", entry="tools/other/tool.py", engine_op="other")
        base_tools.append(other)
        staged_tools.append({**other, "version": "1.2.4"})
    policy_repository(base_tools, staged_tools, changed)
    if error_code is None:
        assert CustomizationService._verify_stage_policy(
            revision_change, {"tool": body_tool},
        ) == ("registry.json", "tools/measure/tool.py", "tools/measure/tool.json")
    else:
        with pytest.raises(CustomizationServiceError) as caught:
            CustomizationService._verify_stage_policy(revision_change, {"tool": body_tool})
        assert caught.value.code == error_code
        assert caught.value.status_code == 422


def test_w22d2_schema_identity_types():
    equal = customization_service._schema_equal
    assert equal({"a": 1, "b": [True, None, "s"]}, {"b": [True, None, "s"], "a": 1}) is True
    assert equal(True, 1) is False
    assert equal(1, 1.0) is False
    assert equal(0.0, -0.0) is False
    assert equal([True], [1]) is False
    assert equal({"x": True}, {"x": 1}) is False
    assert equal(object(), object()) is False


def test_w22d2_schema_identity_non_string_key():
    change = customization_service._revision_schema_change
    base = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert change(base, {"type": "object", "properties": {"x": {"type": "number"}, 1: {}}}) == "breaking"
    assert change(base, {"type": "object", "properties": {"x": {"type": "number"}, "y": {}}}) == "additive"
