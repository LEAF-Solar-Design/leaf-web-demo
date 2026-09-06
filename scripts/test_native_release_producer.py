"""Recipe unit tests. No Docker builds or provider operations occur here."""
from pathlib import Path

import pytest

from ci.native_release_producer import image_build_command, SERVICES, FRESHNESS, TRIXIE


def recipe(service, **kwargs):
    freshness = {name: "b" * 64 for name in FRESHNESS.get(service, TRIXIE)}
    return image_build_command(service, "a" * 40, 7, freshness, Path("image.json"), **kwargs)


@pytest.mark.parametrize("service", SERVICES)
def test_preserves_image_recipe_and_uses_native_tag(service):
    command = recipe(service, **({"solver_revision": "c" * 40} if service == "canonical-worker" else {}))
    assert command[:4] == ["docker", "buildx", "build", "--pull"]
    assert f"deploy/Dockerfile.{service}" in command
    assert "linux/amd64" in command
    assert command[-1] == "."
    assert f":native-7-{'a' * 40}" in command[-2]
    assert command.count("--build-context") == (7 if service == "canonical-worker" else 6)
    if service == "app":
        assert "--push" not in command
        assert "type=image,push=true,compression=zstd,force-compression=true,oci-mediatypes=true" in command
    else:
        assert "--push" in command


def test_solver_is_required_not_invented():
    with pytest.raises(ValueError, match="solver"):
        recipe("canonical-worker")


def test_missing_freshness_cannot_silently_build():
    with pytest.raises(ValueError, match="freshness"):
        image_build_command("app", "a" * 40, 7, {}, Path("image.json"))


def test_invalid_source_cannot_enter_shell_or_tag():
    with pytest.raises(ValueError, match="source"):
        image_build_command("app", "main;echo unsafe", 7, {}, Path("image.json"))


def test_bool_is_not_a_native_build_number():
    with pytest.raises(ValueError, match="number"):
        image_build_command("app", "a" * 40, True, {}, Path("image.json"))
