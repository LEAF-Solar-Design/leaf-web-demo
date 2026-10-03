"""Keep the app image version wired to the telemetry sink's first choice."""

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def version_env_block():
    lines = (REPO_ROOT / "deploy" / "Dockerfile.app").read_text(
        encoding="utf-8"
    ).splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if not line.startswith("ENV "):
            continue
        block = [line]
        for following in lines[index + 1 :]:
            if not following or not following[0].isspace():
                break
            block.append(following)
        assignments = " ".join(block).replace("\\", "").split()
        if "LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA}" in assignments:
            blocks.append(block)
    assert len(blocks) == 1, "Expected one ENV block exporting LEAF_SOURCE_SHA"
    return blocks[0]


def test_app_version_uses_source_sha_in_runtime_env(version_env_block):
    assignments = " ".join(version_env_block).replace("\\", "").split()
    assert "LEAF_APP_VERSION=${LEAF_SOURCE_SHA}" in assignments


def test_runtime_env_continuations_are_valid(version_env_block):
    assert all(line.endswith("\\") for line in version_env_block[:-1])
    assert not version_env_block[-1].endswith("\\")


def test_telemetry_sink_reads_app_version_first():
    source = (REPO_ROOT / "server" / "telemetry_sink.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_app_version"
    ]
    assert len(functions) == 1
    returns = [node for node in functions[0].body if isinstance(node, ast.Return)]
    assert len(returns) == 1
    expected = ast.parse(
        'os.environ.get("LEAF_APP_VERSION") or os.environ.get("GIT_SHA") or None',
        mode="eval",
    ).body
    assert ast.dump(returns[0].value) == ast.dump(expected)
