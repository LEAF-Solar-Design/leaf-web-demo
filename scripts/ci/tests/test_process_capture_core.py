"""Dormant process-capture contracts; no Linux tracer or network is required."""

import copy
import importlib
from pathlib import Path

import pytest


CI_DIR = Path(__file__).resolve().parents[1]
MODULE_NAMES = ("trace_process_tree", "trace_supervisor", "external_inventory", "epoch_probe")
EPOCH_FUNCTIONS = ("normalize_root_name", "_normalize_epoch_manifest", "normalized_epoch")


@pytest.fixture
def modules(monkeypatch):
    # Exercise the plain-module fallback used by the CI entrypoints.
    monkeypatch.syspath_prepend(str(CI_DIR))
    return {name: importlib.import_module(name) for name in (*MODULE_NAMES, "select_tests")}


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_plain_module_import(modules, name):
    module = modules[name]
    assert Path(module.__file__).resolve() == CI_DIR / (name + ".py")
    assert module.__package__ == ""


@pytest.mark.parametrize("name", EPOCH_FUNCTIONS)
def test_supervisor_reexports_selector_epoch_functions(modules, name):
    assert getattr(modules["trace_supervisor"], name) is getattr(modules["select_tests"], name)


@pytest.mark.parametrize("first,second,folded", [
    ("tmp.ABCDEF1234", "tmp.9876543210", "tmp.*"),
    ("leaf-selection.ABCDEF12", "leaf-selection.87654321", "leaf-selection.*"),
    ("relay-src.ABC123", "relay-src.654321", "relay-src.*"),
])
def test_normalized_epoch_is_deterministic(modules, first, second, folded):
    selector = modules["select_tests"]
    root = "/tmp/" + first + "/lib"
    manifest = {
        "external_root_table": {root: {"class": "python-installation"}},
        "external_root_digests": {root: {"sha256": "a" * 64}},
        "reporting_helper_sha256s": {"select_tests.py": "b" * 64, "trace_supervisor.py": "c" * 64},
        "parser_version": "s15b-6",
    }
    original = copy.deepcopy(manifest)
    renamed = copy.deepcopy(manifest)
    for field in ("external_root_table", "external_root_digests"):
        renamed[field] = {"/tmp/" + second + "/lib": manifest[field][root]}
    assert selector.normalize_root_name(root) == "/tmp/" + folded + "/lib"
    normalized = selector._normalize_epoch_manifest(manifest)
    for field in ("external_root_table", "external_root_digests"):
        assert normalized[field] == {"/tmp/" + folded + "/lib": manifest[field][root]}
    epoch = selector.normalized_epoch(manifest)
    assert len(epoch) == 64
    assert epoch == selector.normalized_epoch(copy.deepcopy(manifest))
    assert epoch == selector.normalized_epoch(renamed)
    assert manifest == original
    renamed["parser_version"] = "changed-parser"
    assert epoch != selector.normalized_epoch(renamed)


def test_inventory_has_no_terraform_roots_or_classes(modules):
    inventory = modules["external_inventory"]
    constants = {name: value for name, value in vars(inventory).items()
                 if name.isupper() and not name.startswith("_")}
    assert "terraform" not in repr(constants).lower()
    assert "TF_PLUGIN_CACHE_DIR" not in repr(constants)
    assert inventory.ADMISSIBLE_CLASSES == ("python-installation", "os-image")
    assert "terraform" not in repr(modules["trace_supervisor"].ADMISSION_POLICY).lower()


@pytest.mark.parametrize("path", [
    "/usr/share/zoneinfo", "/etc/timezone", "/usr/local/bin",
    "/usr/lib/git-core/git", "/usr/lib/git-core/git-upload-pack",
    "/usr/lib/git-core/git-receive-pack", "/etc/mime.types", "/etc/gai.conf",
    "/etc/os-release", "/usr/lib/os-release", "/etc/python3.12", "/etc/fonts",
    "/usr/share/fonts", "/usr/local/share/fonts", "/var/cache/fontconfig",
    "/usr/lib/node_modules", "/usr/lib/postgresql", "/root/.npm",
])
def test_inventory_has_capture_2_image_roots(modules, path):
    expected_class = "generated" if path == "/root/.npm" else "os-image"
    assert modules["external_inventory"].DEFAULT_ROOTS[path] == {
        "class": expected_class, "origin_category": expected_class,
    }


@pytest.mark.parametrize("root_name", ["node_modules", "installed"])
def test_inventory_runtime_cache_scope(modules, tmp_path, root_name):
    inventory = modules["external_inventory"]
    root = tmp_path / root_name
    root.mkdir()
    (root / "package.json").write_text("installed bytes", encoding="utf-8")
    before = inventory.digest_root(str(root))
    assert before["complete"] is True
    assert before["files"] == 1
    for relative in (".leaf-ci/x.mjs", ".vite/vitest/h/results.json", ".vite-temp/t.mjs"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime bytes", encoding="utf-8")
        after = inventory.digest_root(str(root))
        assert after["complete"] is True
        if root_name == "node_modules":
            assert after == before
        else:
            assert after["sha256"] != before["sha256"]
    assert after["files"] == (1 if root_name == "node_modules" else 4)


def test_inventory_nested_runtime_named_directory_counts(modules, tmp_path):
    root = tmp_path / "node_modules"
    root.mkdir()
    before = modules["external_inventory"].digest_root(str(root))
    path = root / "pkg" / ".vite" / "file"
    path.parent.mkdir(parents=True)
    path.write_text("installed nested bytes", encoding="utf-8")
    after = modules["external_inventory"].digest_root(str(root))
    assert after["complete"] is True
    assert after["files"] == 1
    assert after["sha256"] != before["sha256"]


# Documented strace -f -ttt -x line formats: a normal call and its split equivalent.
@pytest.mark.parametrize("transcript,kinds", [
    (b'123 1700000000.000001 openat(AT_FDCWD, "config.json", O_RDONLY|O_CLOEXEC) = 3\n',
     ["syscall"]),
    (b'123 1700000000.000001 openat(AT_FDCWD, "config.json", O_RDONLY|O_CLOEXEC <unfinished ...>\n'
     b'123 1700000000.000002 <... openat resumed>) = 3\n',
     ["unfinished", "resumed"]),
])
def test_decoder_parses_openat_fixtures(modules, transcript, kinds):
    decoder = modules["trace_process_tree"]
    events, reasons = decoder.parse_stream(transcript)
    assert reasons == []
    assert [event["kind"] for event in events] == kinds
    assert events[0]["name"] == "openat"
    assert events[0]["paths"] == {1: "config.json"}
    assert "O_RDONLY" in events[0]["flags"]
    assert events[-1]["return_value"] == 3
    assert decoder.PARSER_VERSION == "s15b-6-wd1"


@pytest.mark.parametrize("transcript,expected_size", [
    (b'123 1700000000.000001 newfstatat(AT_FDCWD, "/usr/local/bin/python", '
     b'{st_mode=S_IFLNK|0777, st_size=18, ...}, AT_SYMLINK_NOFOLLOW) = 0\n', None),
    (b'123 1700000000.000001 newfstatat(AT_FDCWD, "/usr/local/bin/python", '
     b'{st_mode=S_IFREG|0755, st_size=6000, ...}, AT_SYMLINK_NOFOLLOW) = 0\n', 6000),
    (b'123 1700000000.000001 newfstatat(AT_FDCWD, "/usr/local/bin/python", '
     b'{st_mode=S_IFDIR|0755, st_size=4096, ...}, AT_SYMLINK_NOFOLLOW) = 0\n', 4096),
    (b'123 1700000000.000001 statx(AT_FDCWD, "/usr/local/bin/python", '
     b'AT_SYMLINK_NOFOLLOW, STATX_BASIC_STATS, '
     b'{stx_mode=S_IFLNK|0777, stx_size=18, ...}) = 0\n', None),
    (b'123 1700000000.000001 statx(AT_FDCWD, "/usr/local/bin/python", '
     b'AT_SYMLINK_NOFOLLOW, STATX_BASIC_STATS, '
     b'{stx_mode=S_IFREG|0755, stx_size=6000, ...}) = 0\n', 6000),
])
def test_decoder_stat_size_excludes_symlinks(modules, transcript, expected_size):
    events, reasons = modules["trace_process_tree"].parse_stream(transcript)
    assert reasons == []
    assert len(events) == 1
    event = events[0]
    assert event["paths"] == {1: "/usr/local/bin/python"}
    assert event["return_value"] == 0
    if expected_size is None:
        assert "observed_size" not in event
    else:
        assert event["observed_size"] == expected_size


def test_supervisor_help_exits_zero(modules, capsys):
    with pytest.raises(SystemExit) as result:
        modules["trace_supervisor"].main(["--help"])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out
