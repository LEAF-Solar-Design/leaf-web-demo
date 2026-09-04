"""The digest read in published_tool_source_sha256 is provably inside an allowed root.

resolve_local_file already contains the path (F4), but the second, local check is the
one a static scan can see and the one this file pins: a resolved path OUTSIDE every
allowed root is never read, a path inside SERVER_DIR is hashed, and a symlink that
points out of the root is measured at its target and refused.
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tool_loader  # noqa: E402


def _tool():
    return {"id": "t", "entry": "tools/t/tool.py"}


def test_a_path_outside_every_root_is_never_read(tmp_path, monkeypatch):
    outside = tmp_path / "secret.py"
    outside.write_text("print('no')", encoding="utf-8")
    monkeypatch.setattr(tool_loader, "resolve_local_file", lambda tool, tenant_id=None: outside)
    monkeypatch.setattr(tool_loader, "is_trusted_builtin_tool", lambda tool, tenant_id=None: False)
    assert tool_loader.published_tool_source_sha256(_tool()) is None


def test_a_path_inside_the_server_dir_is_hashed(tmp_path, monkeypatch):
    inside_root = tool_loader.SERVER_DIR / "authored"
    inside_root.mkdir(parents=True, exist_ok=True)
    target = inside_root / "_containment_probe_tool.py"
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    try:
        monkeypatch.setattr(tool_loader, "resolve_local_file", lambda tool, tenant_id=None: target)
        monkeypatch.setattr(tool_loader, "is_trusted_builtin_tool", lambda tool, tenant_id=None: False)
        # The digest is over the TEXT-MODE view (CRLF collapsed), the same bytes the
        # sandbox tiers are fed, not over the raw file.
        expected = hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert tool_loader.published_tool_source_sha256(_tool()) == expected
    finally:
        target.unlink(missing_ok=True)


def test_root_prefix_needs_a_separator(tmp_path, monkeypatch):
    # `<root>2/x.py` must not pass as inside `<root>`.
    sibling = Path(str(tool_loader.SERVER_DIR) + "2")
    monkeypatch.setattr(tool_loader, "_published_source_roots", lambda tenant_id=None: [tool_loader.SERVER_DIR])
    assert tool_loader._contained_published_path(sibling / "x.py") is None
    assert tool_loader._contained_published_path(tool_loader.SERVER_DIR / "x.py") is not None


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs a privilege on Windows")
def test_a_symlink_out_of_the_root_is_refused(tmp_path, monkeypatch):
    outside = tmp_path / "secret.py"
    outside.write_text("x = 1", encoding="utf-8")
    link = tool_loader.SERVER_DIR / "authored" / "_containment_probe_link.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, link)
        monkeypatch.setattr(tool_loader, "resolve_local_file", lambda tool, tenant_id=None: link)
        monkeypatch.setattr(tool_loader, "is_trusted_builtin_tool", lambda tool, tenant_id=None: False)
        assert tool_loader.published_tool_source_sha256(_tool()) is None
    finally:
        if link.is_symlink():
            link.unlink()
