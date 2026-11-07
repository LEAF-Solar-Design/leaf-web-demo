import hashlib
import importlib.util
from pathlib import Path
import shutil
import tarfile


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_package", SOURCE / "build_package.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_two_builds_are_byte_identical_and_metadata_is_fixed(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(SOURCE / "leaf_agent", source / "leaf_agent")
    shutil.copyfile(SOURCE / "requirements.lock", source / "requirements.lock")
    shutil.copyfile(SOURCE / "install.sh.tmpl", source / "install.sh.tmpl")
    cache = source / "leaf_agent/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "fake.pyc").write_bytes(b"not release material")
    first, second = tmp_path / "first", tmp_path / "second"
    digest = builder.build(source, first)
    assert builder.build(source, second) == digest
    archive_bytes = (first / "package.tar.gz").read_bytes()
    assert archive_bytes == (second / "package.tar.gz").read_bytes()
    assert digest == hashlib.sha256(archive_bytes).hexdigest()
    assert archive_bytes[4:8] == b"\x00" * 4
    assert (first / "install.sh").read_bytes() == (second / "install.sh").read_bytes()
    with tarfile.open(first / "package.tar.gz") as archive:
        entries = archive.getmembers()
        names = [entry.name for entry in entries]
        assert names == sorted(names)
        assert "requirements.lock" in names and "leaf_agent/__main__.py" in names
        assert all(name == "requirements.lock" or name.startswith("leaf_agent/") for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert all(entry.mtime == entry.uid == entry.gid == 0 and entry.mode == 0o644
                   and entry.uname == entry.gname == "" for entry in entries)
    installer = (first / "install.sh").read_text(encoding="utf-8")
    assert digest in installer and "@SHA256@" not in installer and "@VERSION@" not in installer
    assert "Leaf device agent 0.1.1" in installer


def test_installer_and_lock_preserve_release_guards():
    text = (SOURCE / "install.sh.tmpl").read_text(encoding="utf-8")
    assert "set -eu" in text and "LEAF_ENROLL_TOKEN" in text
    assert "--require-hashes" in text and "--only-binary=:all:" in text
    assert "--token" not in text and "--insecure" not in text
    assert text.index("Package checksum mismatch") < text.index("-m venv")
    assert "/opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3" in text
    assert "brew install python@3.13" in text
    assert "--proto '=https'" in text
    requirements = (SOURCE / "requirements.lock").read_text(encoding="utf-8")
    assert "cryptography==50.0.0" in requirements
    assert "cffi==2.0.0" in requirements and "pycparser==2.23" in requirements
    assert "--hash=sha256:" in requirements
