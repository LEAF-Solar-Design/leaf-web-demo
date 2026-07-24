import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_worker_required_config_is_headless_and_database_only():
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.worker.json").read_text(encoding="utf-8")
    )

    assert manifest == {
        "schemaVersion": 1,
        "container": "leaf-platform-worker",
        "required": {
            "environment": [
                "AUTOFILL_SOLVER_REVISION",
                "LEAF_CANONICAL_LEASE_SECONDS",
            ],
            "mountPaths": [],
            "secrets": ["DATABASE_URL"],
        },
    }


def test_worker_image_does_not_install_conflicting_solver_service_requirements():
    dockerfile = (ROOT / "deploy" / "Dockerfile.canonical-worker").read_text(
        encoding="utf-8"
    )
    assert "COPY --from=autofill_solver . /opt/leaf/autofill-solver/" in dockerfile
    assert "autofill-requirements.txt" not in dockerfile
    assert "> /opt/leaf/autofill-solver/.leaf-source-revision" in dockerfile


def test_compose_supplies_exact_solver_revision_build_arg():
    overlay = (ROOT / "docker-compose.canonical.yml").read_text(encoding="utf-8")

    assert overlay.count("AUTOFILL_SOLVER_REVISION: ${AUTOFILL_SOLVER_REVISION:?") == 2
