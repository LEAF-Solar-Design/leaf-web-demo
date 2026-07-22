import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("app", "web", "broker", "harness")
REQUIRED_KEYS = ("environment", "secrets", "mountPaths")


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
