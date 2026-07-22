import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("app", "web", "broker", "harness")
REQUIRED_KEYS = ("environment", "secrets", "mountPaths")
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "web-deployment-contract.yml",
    ROOT / ".github" / "workflows" / "platform-backend-contract.yml",
)


def test_every_platform_service_has_a_well_formed_required_config_manifest() -> None:
    for service in SERVICES:
        path = ROOT / "deploy" / f"required-config.{service}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["schemaVersion"] == 1
        assert manifest["container"] == f"leaf-platform-{service}"
        assert set(manifest["required"]) == set(REQUIRED_KEYS)
        for key in REQUIRED_KEYS:
            values = manifest["required"][key]
            assert isinstance(values, list)
            assert all(isinstance(value, str) and value for value in values)
            assert values == sorted(set(values))


def test_manifests_pin_the_existing_runtime_contract() -> None:
    app = json.loads((ROOT / "deploy" / "required-config.app.json").read_text())
    broker = json.loads((ROOT / "deploy" / "required-config.broker.json").read_text())
    harness = json.loads((ROOT / "deploy" / "required-config.harness.json").read_text())

    assert "DATABASE_URL" in app["required"]["secrets"]
    assert "LEAF_BROKER_SECRET" in app["required"]["secrets"]
    assert "APS_CREDENTIALS_JSON" in broker["required"]["secrets"]
    assert "LEAF_HARNESS_SECRET" in harness["required"]["secrets"]
    assert "/data/state" in app["required"]["mountPaths"]
    assert "/data/grants" in harness["required"]["mountPaths"]


def test_both_required_deployment_checks_run_for_every_main_commit() -> None:
    expected_jobs = ("contract", "public-site-api")
    for workflow, expected_job in zip(WORKFLOWS, expected_jobs, strict=True):
        text = workflow.read_text(encoding="utf-8")
        push_block = text.split("  push:\n", 1)[1].split("\n\npermissions:", 1)[0]
        assert "branches: [main]" in push_block
        assert "paths:" not in push_block
        assert f"\n  {expected_job}:\n" in text


def test_deployment_inputs_trigger_pull_request_checks() -> None:
    web = WORKFLOWS[0].read_text(encoding="utf-8")
    backend = WORKFLOWS[1].read_text(encoding="utf-8")
    web_pr = web.split("  pull_request:\n", 1)[1].split("  push:\n", 1)[0]
    backend_pr = backend.split("  pull_request:\n", 1)[1].split("  push:\n", 1)[0]

    assert '- "deploy/**"' in web_pr
    assert '- "deploy/**"' in backend_pr
    assert '- "scripts/test_required_config.py"' in backend_pr


def test_application_image_carries_the_exact_source_identity() -> None:
    dockerfile = (ROOT / "deploy" / "Dockerfile.app").read_text(encoding="utf-8")
    workflow = WORKFLOWS[1].read_text(encoding="utf-8")
    app = (ROOT / "server" / "app.py").read_text(encoding="utf-8")

    assert "ARG LEAF_SOURCE_SHA=unknown" in dockerfile
    assert "ENV LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA}" in dockerfile
    assert '--build-arg LEAF_SOURCE_SHA="$LEAF_SOURCE_SHA"' in workflow
    assert '"source_sha": os.environ.get("LEAF_SOURCE_SHA"' in app
