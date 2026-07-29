#!/usr/bin/env python3
"""Static contract checks for the warm instant execution container."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "Dockerfile.instant-execution"
DEPLOY_DOC = ROOT / "deploy" / "README.md"
CONTROL_PLANE_REQUIREMENTS = ROOT / "executor" / "control_plane" / "requirements.txt"
RUNTIME_REQUIREMENTS = ROOT / "executor" / "runtime" / "requirements.txt"


def main() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    documentation = DEPLOY_DOC.read_text(encoding="utf-8")
    control_plane_requirements = CONTROL_PLANE_REQUIREMENTS.read_text(encoding="utf-8")
    runtime_requirements = RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in text
    assert "executor/control_plane/requirements.txt" in text
    assert "executor/runtime/requirements.txt" in text
    assert "COPY ." not in text
    assert "COPY executor/ " not in text
    for required_copy in (
        "executor/bootstrap.py",
        "executor/control_plane/*.py",
        "executor/runtime/*.py",
        "executor/contracts/*.py",
        "executor/contracts/schemas/",
    ):
        assert required_copy in text

    assert "ARG LEAF_SOURCE_SHA" in text
    assert "LEAF_SOURCE_SHA must be an exact lowercase 40-character commit" in text
    assert "ca-certificates" in text
    assert "/etc/ssl/certs/ca-certificates.crt" in (ROOT / "executor" / "control_plane" / "production.py").read_text(encoding="utf-8")
    assert "org.opencontainers.image.revision=$LEAF_SOURCE_SHA" in text
    assert "USER 65532:65532" in text
    for required_env in ("HOME=/tmp", "TMPDIR=/tmp", "XDG_CACHE_HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"):
        assert required_env in text
    assert '"--worker-tmp-dir", "/tmp"' in text
    assert "--read-only --tmpfs /tmp" in documentation

    assert "gunicorn" in text
    assert "executor.control_plane.production:create_wsgi_application()" in text
    assert "python -m executor.control_plane.reaper_main" in documentation
    assert "python -m executor.runtime.service" in documentation
    assert "python -m executor.bootstrap control" in documentation
    assert "python -m executor.bootstrap executor" in documentation
    assert "mode `0600`" in documentation
    assert "cryptography>=48.0.1,<49" in control_plane_requirements
    assert "cryptography>=48.0.1,<49" in runtime_requirements

    forbidden = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "COPY .aws",
        "COPY .env",
        "COPY secrets",
        "COPY .secrets",
    )
    for value in forbidden:
        assert value not in text

    print("instant execution container contract: PASS")


def test_instant_execution_container_contract() -> None:
    main()


if __name__ == "__main__":
    main()
