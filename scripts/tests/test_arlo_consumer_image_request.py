"""ARLO image source contract, without building, AWS access or solver execution."""
import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "arlo_image_request_under_test", ROOT / "scripts/ci/arlo_consumer_image_request.py")
REQUEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REQUEST)


def test_platform_pin_matches_published_reference_capable_api_source():
    # The previous image baked f20fa02b, whose worker only accepts synthetic-lab
    # provenance. This exact published source includes the reference producer,
    # reference-aware reader and numeric-serialization correction together.
    assert REQUEST.PINS["platform_pinned"] == (
        "leaf-web-demo", "91c8bf8ad32fb3f6e6308d56987a0599bd85b819")


def test_build_worktrees_consume_exact_declared_sources():
    buildspec = (ROOT / ".codebuild/arlo-consumer-image.yml").read_text()
    for identifier in ("platform_pinned", "arlo_source"):
        matches = re.findall(
            r'worktree add --detach "\$CODEBUILD_SRC_DIR_' + identifier + r'" ([0-9a-f]{40})',
            buildspec)
        assert matches == [REQUEST.PINS[identifier][1]]
    assert REQUEST.PINS["arlo_source"] == (
        "arlo-3dml", "f35a10e864879ac0723fe595fc8dbbe02e09b5f2")
    assert REQUEST.PINS["arlo_recipe"] == (
        "arlo-3dml", "acd89a0a413774147c3aa12607372d0d9e5b209e")


def test_request_emits_only_image_build_inputs_without_submitting():
    producer = "a" * 40
    request = REQUEST.request(producer)
    assert request["sourceVersion"] == producer
    assert request["buildspecOverride"] == ".codebuild/arlo-consumer-image.yml"
    assert request["secondarySourcesVersionOverride"] == [{
        "sourceIdentifier": "arlo_recipe", "sourceVersion": REQUEST.PINS["arlo_recipe"][1]}]
    assert "environmentVariablesOverride" not in request


@pytest.mark.parametrize("producer", ["main", "a" * 39, "a" * 41, "G" * 40])
def test_request_refuses_unbound_producer(producer):
    with pytest.raises(ValueError, match="exact published commit"):
        REQUEST.request(producer)
