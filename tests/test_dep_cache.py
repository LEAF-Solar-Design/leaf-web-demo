"""Deterministic dependency cache contracts; no installers or network."""

import builtins
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

import pytest


SPEC = importlib.util.spec_from_file_location(
    "leaf_dep_cache", Path(__file__).resolve().parents[1] / "scripts/ci/dep_cache.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home")})
    repo = tmp_path / "repo"
    for rel in cache.DEPENDENCY_FILES:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pytest>=8\n")
    (repo / "web/package-lock.json").write_text(json.dumps({"packages": {
        "node_modules/playwright-core": {"version": "1.60.0", "integrity": "sha512-fixture"}}}))
    (repo / "server/requirements.txt").write_text("playwright==1.60.0\npytest\n")
    (repo / "web/next.config.mjs").write_text("export default {};\n")
    for tree in ("web", "harness"):
        (repo / tree / "package.json").write_text('{"private":true}\n')
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    argv = ["--repo", str(repo), "--cache-dir", str(tmp_path / "cache"),
            "--run", "device:fixture", "--writable", "1", "--day", "20261111",
            "--node-version", "v22.1.0", "--npm-version", "10.9.0", "--tree", "web",
            "--os-release", str(release),
            "--pip-cache", str(tmp_path / "pip"), "--npm-cache", str(tmp_path / "npm"),
            "--revision", "a" * 40, "--src", str(tmp_path / "snapshot"),
            "--dest", str(tmp_path / "dest")]
    return tmp_path, argv


def arguments(fixture, action="seed", kind="dl", **changes):
    args = cache.parser().parse_args([action, "--kind", kind, *fixture[1]])
    for key, value in changes.items():
        setattr(args, key, str(value))
    return args


def invoke(fixture, capsys, action="seed", kind="dl", **changes):
    args = arguments(fixture, action, kind, **changes)
    argv = [args.action]
    for key, value in vars(args).items():
        if key != "action":
            argv.extend(["--" + key.replace("_", "-"), value])
    assert cache.main(argv) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert re.match(r"^LEAF_DEP_CACHE kind=\S+ action=\S+ result=\S+ ", output)
    return dict(item.split("=", 1) for item in output.split()[1:])


def write(path, content=b"fixture", executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755 if executable else 0o644)


def sources(fixture, kind="dl"):
    root = fixture[0]
    if kind == "dl":
        write(root / "pip/http/body", b"wheel")
        write(root / "npm/_cacache/content/tool", b"tarball", executable=True)
        write(root / "pip/selfcheck/state")
        write(root / "npm/_logs/log")
        write(root / "npm/_npx/transient")
        write(root / "npm/_cacache/tmp/partial")
        write(root / "npm/_update-notifier-last-checked")
        (root / "pip/http/link").symlink_to("body")
    elif kind == "pw":
        browser = root / "home/.cache/ms-playwright"
        write(browser / "chromium-1/INSTALLATION_COMPLETE", b"")
        write(browser / "chromium-1/chrome", b"browser", executable=True)
        write(browser / "chromium-1/DEPENDENCIES_VALIDATED")
        write(browser / ".links/old")
        write(browser / "__dirlock")
    else:
        write(root / "snapshot/snapshot.bundle", b"git bundle")
        write(root / "snapshot/manifest.json", json.dumps({"head_sha": "a" * 40,
              "repository": "LEAF-Solar-Design/autofill-solver"}).encode())


def entry_for(fixture, kind="dl", **changes):
    args = arguments(fixture, kind=kind, **changes)
    return Path(args.cache_dir) / f"{kind}-v1-{cache.digest(cache.key_inputs(args))}"


def snapshot(root):
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix():
            (p.lstat().st_mode, p.lstat().st_mtime_ns,
             os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in [root, *root.rglob("*")]}


def test_dl_key_stable_and_sensitive(fixture):
    args = arguments(fixture)
    original = cache.key_inputs(args)
    assert cache.key_inputs(args) == original
    for rel in cache.DEPENDENCY_FILES:
        path = Path(args.repo) / rel
        content = path.read_bytes()
        path.write_bytes(content + b"x")
        assert cache.key_inputs(args) != original
        path.write_bytes(content)
    write(Path(args.repo) / "unrelated")
    assert cache.key_inputs(args) == original
    assert cache.key_inputs(arguments(fixture, day="20261112")) != original
    assert cache.key_inputs(arguments(fixture, node_version="v20.0.0")) != original
    assert cache.key_inputs(arguments(fixture, node_version="v22.9.9")) == original


def test_nm_key_stable_and_sensitive(fixture):
    args = arguments(fixture, kind="nm")
    original = cache.key_inputs(args)
    assert cache.key_inputs(args) == original
    assert set(original) == {"schema", "tree", "lock", "package", "node", "npm", "platform", "arch", "os"}
    for name in ("package-lock.json", "package.json"):
        path = Path(args.repo) / "web" / name
        content = path.read_bytes()
        path.write_bytes(content + b"x")
        assert cache.key_inputs(args) != original
        path.write_bytes(content)
        path.unlink()
        with pytest.raises(ValueError):
            cache.key_inputs(args)
        path.write_bytes(content)
        # Identical files still belong to separate tree keys.
        write(Path(args.repo) / "harness" / name, content)
    for changes in ({"node_version": "v22.1.1"}, {"npm_version": "10.9.1"}, {"tree": "harness"}):
        assert cache.key_inputs(arguments(fixture, kind="nm", **changes)) != original
    args.tree = None
    with pytest.raises(ValueError):
        cache.key_inputs(args)


@pytest.mark.parametrize("field,value", [
    ("node_version", "22.1.0"), ("node_version", "v22"), ("node_version", "v22.1.0-extra"),
    ("npm_version", "v10.9.0"), ("npm_version", "10.9"), ("npm_version", "10.9.0\n"),
])
def test_nm_bad_versions_fail_open(fixture, capsys, field, value):
    assert invoke(fixture, capsys, kind="nm", **{field: value})["result"] == "error"
    assert not (fixture[0] / "cache").exists()


def test_nm_installed_tree_round_trip(fixture, capsys):
    root = fixture[0] / "repo/web/node_modules"
    write(root / "tool/bin/run", b"#!/bin/sh\n", executable=True)
    write(root / "tool/index.js", b"module.exports = 1;")
    (root / ".bin").mkdir()
    (root / ".bin/tool").symlink_to("../tool/bin/run")
    for name in (".cache", ".vite", ".vitest"):
        write(root / name / "transient")
    assert invoke(fixture, capsys, "publish", "nm")["result"] == "published"
    cache.shutil.rmtree(root)
    result = invoke(fixture, capsys, kind="nm", writable="0")
    assert result["result"] == "exact" and result["node_modules"] == "seeded"
    assert (root / "tool/bin/run").read_bytes() == b"#!/bin/sh\n"
    assert (root / "tool/bin/run").stat().st_mode & 0o111
    assert (root / "tool/index.js").read_bytes() == b"module.exports = 1;"
    assert not (root / "tool/index.js").stat().st_mode & 0o111
    assert os.readlink(root / ".bin/tool") == "../tool/bin/run"
    for name in (".cache", ".vite", ".vitest"):
        assert not (root / name).exists()
    assert invoke(fixture, capsys, kind="nm", writable="0", npm_version="10.9.1")["result"] == "miss"


def test_nx_key_stable_and_sensitive(fixture, monkeypatch):
    args = arguments(fixture, kind="nx")
    original = cache.key_inputs(args)
    assert cache.key_inputs(args) == original
    assert original == {
        "schema": "leaf.dep-cache.nx.v1",
        "lock": hashlib.sha256((Path(args.repo) / "web/package-lock.json").read_bytes()).hexdigest(),
        "config": hashlib.sha256((Path(args.repo) / "web/next.config.mjs").read_bytes()).hexdigest(),
        "node": "v22.1.0", "platform": cache.sysconfig.get_platform(),
        "arch": cache.platform.machine(), "day": "20261111",
    }
    for rel in ("web/package-lock.json", "web/next.config.mjs"):
        path = Path(args.repo) / rel
        content = path.read_bytes()
        path.write_bytes(content + b"x")
        assert cache.key_inputs(args) != original
        path.write_bytes(content)
    for changes in ({"node_version": "v22.1.1"}, {"day": "20261112"}):
        assert cache.key_inputs(arguments(fixture, kind="nx", **changes)) != original
    for module, name in ((cache.sysconfig, "get_platform"), (cache.platform, "machine")):
        with monkeypatch.context() as context:
            context.setattr(module, name, lambda: "fixture-other")
            assert cache.key_inputs(args) != original
    write(Path(args.repo) / "web/app/page.tsx", b"changed source")
    Path(args.os_release).unlink()
    assert cache.key_inputs(args) == original


@pytest.mark.parametrize("field,value", [
    ("day", ""), ("day", "2026-11-11"), ("day", "2026111"), ("day", "20261111\n"),
    ("node_version", "22.1.0"), ("node_version", "v22"),
    ("node_version", "v22.1.0-extra"), ("node_version", "v22.1.0\n"),
])
def test_nx_invalid_inputs_fail_open(fixture, capsys, field, value):
    with pytest.raises(ValueError):
        cache.key_inputs(arguments(fixture, kind="nx", **{field: value}))
    assert invoke(fixture, capsys, kind="nx", **{field: value})["result"] == "error"
    assert not (fixture[0] / "cache").exists()


@pytest.mark.parametrize("rel", ["web/package-lock.json", "web/next.config.mjs"])
def test_nx_missing_inputs_fail_open(fixture, capsys, rel):
    (fixture[0] / "repo" / rel).unlink()
    with pytest.raises(ValueError):
        cache.key_inputs(arguments(fixture, kind="nx"))
    assert invoke(fixture, capsys, kind="nx")["result"] == "error"
    assert not (fixture[0] / "cache").exists()


def test_nx_round_trip_excludes_other_cache_data(fixture, capsys):
    root = fixture[0] / "repo/web/.next/cache"
    payloads = {"webpack/client/0.pack": b"webpack client", "webpack/server/index.pack": b"webpack server",
                "swc/plugins/tool.wasm": b"swc"}
    for rel, payload in payloads.items():
        write(root / rel, payload)
    for rel in ("images/image", "fetch-cache/response", "config.json", "webpack-other/partial"):
        write(root / rel)
    assert invoke(fixture, capsys, "publish", "nx")["result"] == "published"
    entry = entry_for(fixture, "nx")
    assert cache.ENTRY_RE.fullmatch(entry.name)
    meta = cache.load_meta(entry, "nx")
    assert {row[0] for row in meta["files"]} == {"next-cache/" + rel for rel in payloads}
    cache.shutil.rmtree(root)
    before = snapshot(entry.parent)
    result = invoke(fixture, capsys, kind="nx", writable="0")
    assert result["result"] == "exact" and result["next-cache"] == "seeded"
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()} == payloads
    assert snapshot(entry.parent) == before
    assert invoke(fixture, capsys, kind="nx", writable="0", day="20261112")["result"] == "miss"


def test_py_key_stable_and_sensitive(fixture, monkeypatch):
    base = fixture[0] / "base"
    base.mkdir()
    (base / "z.dist-info").mkdir()
    (base / "a.egg-info").write_text("metadata")
    (base / "unrelated").mkdir()
    monkeypatch.setattr(cache.sysconfig, "get_paths", lambda: {"purelib": str(base), "platlib": str(base)})
    args = arguments(fixture, kind="py")
    original = cache.key_inputs(args)
    assert cache.key_inputs(args) == original
    assert original["base"] == ["a.egg-info", "z.dist-info"]
    assert set(original["files"]) == set(cache.DEPENDENCY_FILES[2:])
    assert original["executable"] == os.path.realpath(cache.sys.executable)
    for rel in cache.DEPENDENCY_FILES[2:]:
        path = Path(args.repo) / rel
        content = path.read_bytes()
        path.write_bytes(content + b"x")
        assert cache.key_inputs(args) != original
        path.unlink()
        assert cache.key_inputs(args)["files"][rel] == "absent"
        path.write_bytes(content)
    for changes in ({"day": "20261112"}, {"dest": fixture[0] / "other-venv"}):
        assert cache.key_inputs(arguments(fixture, kind="py", **changes)) != original
    assert cache.key_inputs(arguments(fixture, "publish", "py", src=args.dest)) == original
    (base / "new.dist-info").mkdir()
    assert cache.key_inputs(args) != original


@pytest.mark.parametrize("changes", [
    {"day": ""}, {"day": "2026-11-11"}, {"day": "20261111\n"},
    {"dest": ""}, {"dest": "relative/venv"},
])
def test_py_invalid_inputs_fail_open(fixture, capsys, changes):
    assert invoke(fixture, capsys, kind="py", **changes)["result"] == "error"


def test_py_installed_tree_round_trip(fixture, capsys):
    root = fixture[0] / "venv"
    write(root / "pyvenv.cfg")
    write(root / "lib/python/site-packages/tool.py", b"tool = 1\n")
    write(root / "lib/python/site-packages/__pycache__/tool.pyc")
    write(root / "__pycache__/root.pyc")
    write(root / "bin/pytest", b"#!/venv/bin/python\n", executable=True)
    for name in ("python", "python3", "python3.12"):
        (root / "bin" / name).symlink_to("/usr/bin/python3")
    assert invoke(fixture, capsys, "publish", "py", src=root)["result"] == "published"
    cache.shutil.rmtree(root)
    result = invoke(fixture, capsys, kind="py", dest=root, writable="0")
    assert result["result"] == "exact" and result["venv"] == "seeded"
    assert (root / "lib/python/site-packages/tool.py").read_bytes() == b"tool = 1\n"
    assert (root / "bin/pytest").stat().st_mode & 0o111
    assert {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == {
        "lib/python/site-packages/tool.py", "bin/pytest"}
    assert not any(p.is_symlink() or p.name == "__pycache__" for p in root.rglob("*"))
    assert invoke(fixture, capsys, kind="py", dest=root, day="20261112", writable="0")["result"] == "miss"


@pytest.mark.parametrize("kind", ["dl", "pw", "as", "nm", "py", "nx"])
def test_per_kind_off_is_scoped(fixture, capsys, kind):
    write(fixture[0] / "cache" / (".leaf-dep-cache-off-" + kind), b"")
    assert invoke(fixture, capsys, kind=kind)["result"] == "off"
    other = "nm" if kind != "nm" else "py"
    assert invoke(fixture, capsys, kind=other)["result"] == "miss"


def test_pw_key_depends_only_on_versions_os_arch(fixture, monkeypatch):
    args = arguments(fixture, kind="pw")
    original = cache.key_inputs(args)
    write(Path(args.repo) / "harness/package-lock.json", b"unrelated")
    assert cache.key_inputs(args) == original
    lockfile = Path(args.repo) / "web/package-lock.json"
    data = json.loads(lockfile.read_text())
    for field in ("version", "integrity"):
        old = data["packages"]["node_modules/playwright-core"][field]
        data["packages"]["node_modules/playwright-core"][field] = old + "x"
        lockfile.write_text(json.dumps(data))
        assert cache.key_inputs(args) != original
        data["packages"]["node_modules/playwright-core"][field] = old
    lockfile.write_text(json.dumps(data))
    req = Path(args.repo) / "server/requirements.txt"
    old = req.read_text()
    req.write_text("playwright==1.61.0\n")
    assert cache.key_inputs(args) != original
    req.write_text(old)
    with monkeypatch.context() as context:
        context.setattr(cache.platform, "machine", lambda: "fixture-other-arch")
        assert cache.key_inputs(args) != original
    Path(args.os_release).write_text("ID=other\nVERSION_ID=1\n")
    assert cache.key_inputs(args) != original


def test_pw_key_records_absent_python_requirement(fixture):
    args = arguments(fixture, kind="pw")
    requirements = Path(args.repo) / "server/requirements.txt"
    requirements.write_text("pytest\n")
    without_pin = cache.key_inputs(args)
    assert without_pin["python_playwright"] == "absent"
    requirements.unlink()
    assert cache.key_inputs(args) == without_pin


@pytest.mark.parametrize("kind", ["dl", "pw", "as"])
def test_publish_then_exact_seed_round_trip(fixture, capsys, kind):
    sources(fixture, kind)
    result = invoke(fixture, capsys, "publish", kind)
    assert result["result"] == "published"
    root = fixture[0]
    changes = {"pip_cache": root / "new/home/cache/pip", "npm_cache": root / "new/npm"}
    if kind == "pw":
        # Source and destination use the same Playwright registry resolution.
        cache.shutil.rmtree(root / "home/.cache/ms-playwright")
    result = invoke(fixture, capsys, kind=kind, writable="0", **changes)
    assert result["result"] == "exact"
    entry = entry_for(fixture, kind)
    meta = json.loads((entry / "meta.json").read_text())
    destinations = cache.parts(arguments(fixture, kind=kind, **changes))
    for name, size, sha, executable in meta["files"]:
        part, rel = name.split("/", 1)
        path = Path(destinations[part]) / rel
        assert path.read_bytes() == (entry / name).read_bytes()
        assert bool(path.stat().st_mode & 0o111) == executable
    for name, target in meta["symlinks"]:
        part, rel = name.split("/", 1)
        assert os.readlink(Path(destinations[part]) / rel) == target
    assert not any(x in name for name, *_ in meta["files"]
                   for x in ("selfcheck", "_logs", "_npx", "_cacache/tmp", ".links", "__dirlock", "DEPENDENCIES_VALIDATED"))


def test_publish_race_loser_removes_tmp(fixture, capsys, monkeypatch):
    sources(fixture)
    entry = entry_for(fixture)
    rename = os.rename
    def race(source, destination):
        if Path(destination) == entry:
            entry.mkdir()
            write(entry / "winner", b"winner")
            raise OSError(errno.ENOTEMPTY, "concurrent winner")
        return rename(source, destination)
    monkeypatch.setattr(os, "rename", race)
    assert invoke(fixture, capsys, "publish")["result"] == "lost"
    assert (entry / "winner").read_bytes() == b"winner"
    assert not list(entry.parent.glob("*.tmp-*"))


def test_publish_exists_is_noop(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    before = snapshot(fixture[0] / "cache")
    assert invoke(fixture, capsys, "publish")["result"] == "exists"
    assert snapshot(fixture[0] / "cache") == before


def test_publish_refuses_without_writable(fixture, capsys):
    assert invoke(fixture, capsys, "publish", writable="0")["result"] == "refused"
    assert not (fixture[0] / "cache").exists()


@pytest.mark.parametrize("writable", ["0", "1"])
def test_corrupt_entry_falls_back_cold(fixture, capsys, writable):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    entry = entry_for(fixture)
    (entry / "npm/_cacache/content/tool").write_bytes(b"changed")
    before = snapshot(entry.parent)
    result = invoke(fixture, capsys, writable=writable,
                    pip_cache=fixture[0] / "empty/pip", npm_cache=fixture[0] / "empty/npm")
    assert result["result"] == "corrupt"
    assert not list((fixture[0] / "empty").glob("*"))
    if writable == "0":
        assert snapshot(entry.parent) == before
    else:
        assert not entry.exists()
        assert list(entry.parent.glob("*.tmp-corrupt-*"))


def test_readonly_never_writes(fixture, capsys, monkeypatch):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    root = fixture[0] / "cache"
    def guard(function, write_check=lambda a, kw: True):
        def wrapped(path, *args, **kwargs):
            if Path(path).is_relative_to(root) and write_check(args, kwargs):
                pytest.fail("read-only operation wrote cache")
            return function(path, *args, **kwargs)
        return wrapped
    monkeypatch.setattr(os, "rename", guard(os.rename))
    monkeypatch.setattr(os, "utime", guard(os.utime))
    monkeypatch.setattr(os, "mkdir", guard(os.mkdir))
    monkeypatch.setattr(builtins, "open", guard(builtins.open,
                        lambda a, kw: any(c in str(a[0] if a else kw.get("mode", "r")) for c in "wax+")))
    for day, node, expected in (("20261111", "v22", "exact"), ("20261112", "v22", "nearest"),
                                ("20261112", "v20", "miss")):
        result = invoke(fixture, capsys, writable="0", day=day, node_version=node,
                        pip_cache=fixture[0] / day / node / "pip", npm_cache="")
        assert result["result"] == expected
    (entry_for(fixture) / "pip/http/body").write_bytes(b"bad")
    assert invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "corrupt", npm_cache="")["result"] == "corrupt"


def test_nearest_only_for_readonly_dl(fixture, capsys, monkeypatch):
    sources(fixture)
    for day, stamp, node in (("20261109", 10, "v22"), ("20261110", 20, "v22"), ("20261108", 30, "v20")):
        invoke(fixture, capsys, "publish", day=day, node_version=node)
        os.utime(entry_for(fixture, day=day, node_version=node), (stamp, stamp))
    with monkeypatch.context() as context:
        context.setattr(cache.sysconfig, "get_platform", lambda: "other-platform")
        invoke(fixture, capsys, "publish", day="20261107")
        os.utime(entry_for(fixture, day="20261107"), (40, 40))
    root = fixture[0] / "cache"
    (root / "dl-v1-bad").mkdir()
    (root / ("dl-v1-" + "f" * 64 + ".tmp-run")).mkdir()
    result = invoke(fixture, capsys, writable="0", pip_cache="", npm_cache="")
    assert result["result"] == "nearest"
    assert result["entry"] == entry_for(fixture, day="20261110").name
    assert invoke(fixture, capsys, writable="1")["result"] == "miss"
    assert invoke(fixture, capsys, kind="pw", writable="0")["result"] == "miss"


def test_existing_destination_untouched(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    before = snapshot(fixture[0] / "pip")
    result = invoke(fixture, capsys, writable="0")
    assert result["pip"] == "present"
    assert snapshot(fixture[0] / "pip") == before


def test_readonly_nearest_after_invalid_exact_metadata(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish", day="20261110")
    invoke(fixture, capsys, "publish")
    (entry_for(fixture) / "meta.json").write_text("{}")
    before = snapshot(fixture[0] / "cache")
    result = invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "out/pip",
                    npm_cache=fixture[0] / "out/npm")
    assert result["result"] == "nearest"
    assert result["entry"] == entry_for(fixture, day="20261110").name
    assert (fixture[0] / "out/pip/http/body").read_bytes() == b"wheel"
    assert snapshot(fixture[0] / "cache") == before


def test_writable_exact_hit_touches_entry(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    entry = entry_for(fixture)
    os.utime(entry, (10, 10))
    assert invoke(fixture, capsys)["result"] == "exact"
    assert entry.stat().st_mtime > 10


def test_destination_failure_is_error_without_quarantine(fixture, capsys, monkeypatch):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    entry = entry_for(fixture)
    original = os.rename
    def fail_commit(source, destination):
        if str(source).endswith(f".leaf-seed-{os.getpid()}"):
            raise OSError(errno.ENOSPC, "fixture destination full")
        return original(source, destination)
    monkeypatch.setattr(os, "rename", fail_commit)
    result = invoke(fixture, capsys, pip_cache=fixture[0] / "out/pip", npm_cache=fixture[0] / "out/npm")
    assert result["result"] == "error"
    assert entry.is_dir()
    assert not list(entry.parent.glob("*.tmp-corrupt-*"))
    assert not list((fixture[0] / "out").iterdir())


def test_missing_payload_is_corrupt(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    (entry_for(fixture) / "pip/http/body").unlink()
    result = invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "out/pip", npm_cache="")
    assert result["result"] == "corrupt"
    assert not list((fixture[0] / "out").iterdir())


@pytest.mark.parametrize("target", ["/absolute", "../../outside"])
def test_publish_refuses_unsafe_symlinks(fixture, capsys, target):
    sources(fixture)
    (fixture[0] / "pip/unsafe").symlink_to(target)
    assert invoke(fixture, capsys, "publish")["result"] == "refused"
    assert not (fixture[0] / "cache").exists()


def test_readonly_rejects_cache_destination(fixture, capsys):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    before = snapshot(fixture[0] / "cache")
    assert invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "cache/unsafe", npm_cache="")["result"] == "error"
    assert snapshot(fixture[0] / "cache") == before


@pytest.mark.parametrize("attack", ["../x", "/absolute", "pip/nul\x00", "link", "checksum", "ancestor",
                                  "link-path-type", "link-path-nul", "link-path-absolute"])
def test_unsafe_manifest_rejected(fixture, capsys, attack):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    meta_path = entry_for(fixture) / "meta.json"
    meta = json.loads(meta_path.read_text())
    if attack == "link":
        meta["symlinks"] = [["pip/link", "../../escape"]]
    elif attack.startswith("link-path-"):
        name = {"link-path-type": 123, "link-path-nul": "pip/nul\x00",
                "link-path-absolute": "/absolute"}[attack]
        meta["symlinks"] = [[name, "body"]]
    elif attack == "ancestor":
        meta["symlinks"] = [["pip/http", "other"]]
    elif attack != "checksum":
        meta["files"][0][0] = attack
    meta["list_sha256"] = cache.digest({"files": meta["files"], "symlinks": meta["symlinks"]})
    if attack == "checksum":
        meta["list_sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta))
    result = invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "out/pip", npm_cache=fixture[0] / "out/npm")
    assert result["result"] == "corrupt"
    assert not (fixture[0] / "out").exists()


@pytest.mark.parametrize("cap", ["MAX_BYTES", "MAX_FILES"])
def test_bounds_fail_closed(fixture, capsys, monkeypatch, cap):
    sources(fixture)
    invoke(fixture, capsys, "publish")
    monkeypatch.setattr(cache, cap, 1)
    result = invoke(fixture, capsys, writable="0", pip_cache=fixture[0] / "out/pip", npm_cache=fixture[0] / "out/npm")
    assert result["result"] == "error"
    assert not (fixture[0] / "out").exists()
    assert invoke(fixture, capsys, "publish", day="20261112")["result"] == "error"
    assert not list((fixture[0] / "cache").glob("*.tmp-*"))


def test_off_switches(fixture, capsys, monkeypatch):
    root = fixture[0]
    monkeypatch.setenv("LEAF_DEP_CACHE_OFF", "1")
    assert invoke(fixture, capsys)["result"] == "off"
    assert not (root / "cache").exists()
    monkeypatch.delenv("LEAF_DEP_CACHE_OFF")
    write(root / "cache/.leaf-dep-cache-off", b"")
    before = snapshot(root / "cache")
    assert invoke(fixture, capsys)["result"] == "off"
    assert snapshot(root / "cache") == before
    (root / "cache/.leaf-dep-cache-off").unlink()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert invoke(fixture, capsys, kind="pw")["result"] == "off"
    assert invoke(fixture, capsys, cache_dir="")["result"] == "off"


def test_pw_publish_requires_installation_complete(fixture, capsys):
    sources(fixture, "pw")
    (fixture[0] / "home/.cache/ms-playwright/chromium-1/INSTALLATION_COMPLETE").unlink()
    assert invoke(fixture, capsys, "publish", "pw")["result"] == "refused"


@pytest.mark.parametrize("field", ["repository", "head_sha"])
def test_as_publish_checks_manifest_identity(fixture, capsys, field):
    sources(fixture, "as")
    path = fixture[0] / "snapshot/manifest.json"
    data = json.loads(path.read_text())
    data[field] = "wrong"
    path.write_text(json.dumps(data))
    assert invoke(fixture, capsys, "publish", "as")["result"] == "refused"


def test_one_stdout_line_contract(fixture, capsys):
    assert invoke(fixture, capsys)["result"] == "miss"
    assert invoke(fixture, capsys, "publish", writable="0")["result"] == "refused"
    with pytest.raises(SystemExit) as error:
        cache.main(["unknown"])
    assert error.value.code == 2
    assert capsys.readouterr().out == ""
