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


def test_worker_image_installs_only_its_named_solver_requirements():
    dockerfile = (ROOT / "deploy" / "Dockerfile.canonical-worker").read_text(
        encoding="utf-8"
    )
    other_images = "\n".join(
        (ROOT / "deploy" / name).read_text(encoding="utf-8")
        for name in (
            "Dockerfile.app",
            "Dockerfile.broker",
            "Dockerfile.harness",
            "Dockerfile.web",
        )
    )

    assert "COPY --from=autofill_solver requirements.txt" in dockerfile
    assert "-r /app/autofill-requirements.txt" in dockerfile
    assert "> /opt/leaf/autofill-solver/.leaf-source-revision" in dockerfile
    assert "autofill-requirements.txt" not in other_images


def test_compose_supplies_exact_solver_revision_build_arg():
    overlay = (ROOT / "docker-compose.canonical.yml").read_text(encoding="utf-8")

    assert overlay.count("AUTOFILL_SOLVER_REVISION: ${AUTOFILL_SOLVER_REVISION:?") == 2
