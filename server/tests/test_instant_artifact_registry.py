from __future__ import annotations

import hashlib

import pytest

from instant_artifact_registry import (
    ArtifactResolutionError,
    FilesystemTrustedPlatformArtifactRegistry,
)


def _digest(source: bytes) -> str:
    return "sha256:" + hashlib.sha256(source).hexdigest()


def _entry(source: bytes, **changes):
    entry = {
        "name": "instant-read",
        "version": "1.0.0",
        "entry": "tool.py",
        "entrypoint": "tool:run",
        "execution_class": "instant",
        "runtime": "python-3.12",
        "capabilities": ["drawing.read"],
        "params": {"type": "object", "properties": {}},
        "limits": {"max_wall_ms": 100, "max_output_bytes": 1024},
        "code_digest": _digest(source),
        "artifact_digest": _digest(source),
    }
    entry.update(changes)
    return entry


def test_resolves_normalized_trusted_platform_artifact_and_full_binding(tmp_path):
    source = b"def run():\r\n    return 'ok'\r\n"
    root = tmp_path / "instant_tools"
    root.mkdir()
    (root / "tool.py").write_bytes(source)
    normalized = source.replace(b"\r\n", b"\n")
    registry = FilesystemTrustedPlatformArtifactRegistry(root)

    artifact = registry.resolve(_entry(normalized), catalog_commit="a" * 40)

    assert artifact.source == normalized
    assert artifact.tool_id == "instant-read"
    assert artifact.tool_version == "1.0.0"
    assert artifact.execution_class == "instant"
    assert artifact.runtime == "python-3.12"
    assert artifact.capability_id == "drawing.read"
    assert artifact.catalog_commit == "a" * 40
    assert artifact.entrypoint == "tool:run"
    assert artifact.params_schema_digest == _digest(b'{"properties":{},"type":"object"}')
    with pytest.raises(TypeError):
        artifact.limits["max_wall_ms"] = 1


def test_rejects_undeclared_or_mismatched_digests(tmp_path):
    source = b"print('ok')\n"
    root = tmp_path / "instant_tools"
    root.mkdir()
    (root / "tool.py").write_bytes(source)
    registry = FilesystemTrustedPlatformArtifactRegistry(root)

    with pytest.raises(ArtifactResolutionError, match="code_digest"):
        registry.resolve(_entry(source, code_digest=None), catalog_commit="a" * 40)
    with pytest.raises(ArtifactResolutionError, match="does not match"):
        registry.resolve(
            _entry(source, artifact_digest="sha256:" + ("0" * 64)),
            catalog_commit="a" * 40,
        )
    with pytest.raises(ArtifactResolutionError, match="active pin"):
        registry.resolve(
            _entry(source, catalog_commit="b" * 40), catalog_commit="a" * 40,
        )


def test_rejects_absolute_and_traversal_entries(tmp_path):
    source = b"print('ok')\n"
    root = tmp_path / "instant_tools"
    root.mkdir()
    (root / "tool.py").write_bytes(source)
    (root / "not-a-file").mkdir()
    registry = FilesystemTrustedPlatformArtifactRegistry(root)

    for entry in ("../tool.py", "/tmp/tool.py", r"C:\\temp\\tool.py"):
        with pytest.raises(ArtifactResolutionError, match="relative platform path"):
            registry.resolve(_entry(source, entry=entry), catalog_commit="a" * 40)
    with pytest.raises(ArtifactResolutionError, match="outside"):
        registry.resolve(_entry(source, entry="not-a-file"), catalog_commit="a" * 40)


def test_rejects_symlink_escape_where_supported(tmp_path):
    source = b"print('outside')\n"
    root = tmp_path / "instant_tools"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(source)
    escaped = root / "escape.py"
    try:
        escaped.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available in this test environment")

    with pytest.raises(ArtifactResolutionError, match="outside"):
        FilesystemTrustedPlatformArtifactRegistry(root).resolve(
            _entry(source, entry="escape.py"), catalog_commit="a" * 40,
        )


def test_rejects_oversized_invalid_utf8_and_nul_sources(tmp_path):
    root = tmp_path / "instant_tools"
    root.mkdir()
    path = root / "tool.py"
    registry = FilesystemTrustedPlatformArtifactRegistry(root, max_artifact_bytes=8)

    path.write_bytes(b"x" * 9)
    with pytest.raises(ArtifactResolutionError, match="size limit"):
        registry.resolve(_entry(b"x" * 9), catalog_commit="a" * 40)

    path.write_bytes(b"\xff")
    with pytest.raises(ArtifactResolutionError, match="valid UTF-8"):
        registry.resolve(_entry(b"\xff"), catalog_commit="a" * 40)

    path.write_bytes(b"x\x00")
    with pytest.raises(ArtifactResolutionError, match="NUL"):
        registry.resolve(_entry(b"x\x00"), catalog_commit="a" * 40)
