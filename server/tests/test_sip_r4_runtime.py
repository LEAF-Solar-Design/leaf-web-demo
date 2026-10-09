"""R4a: the canonical worker image carries everything the Solar project graph adapter needs.

The canonical worker runs ``python canonical_worker.py`` from ``/app/server`` with only the
distributions declared in ``platform/requirements.txt`` installed (deploy/Dockerfile.canonical-worker).
The graph adapter (server/solar_project_graph.py) needs ``requests`` (solar_sizing_client, da/store.py),
``jsonschema`` (the graph validator's lazy import), ``da/store.py`` and the packaged graph schema under
``contract/``. These rows prove each of those reaches the image, and that the release manifest
fingerprints the two copy roots the image now reads.

Every probe runs in a fresh interpreter whose imports are limited to the interpreter's own library,
the image's local source roots and the dependency closure of a requirements file, so a module that
is installed on the test host but not declared for the image is refused exactly as the image would.
The gate decides by the FILE an import resolves to, never by the import's name: a file an installed
distribution's record lists belongs to that distribution, and a file no record lists is admitted
only from the interpreter's own library directories. A module under a shared namespace, a module
that interpreter startup already imported, and a third-party file that takes a standard-library
name are judged like any other.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "deploy" / "Dockerfile.canonical-worker"
REQUIREMENTS = REPO / "platform" / "requirements.txt"

# A standalone probe, written to a temporary file and run with
# ``python -B <probe> <root> <requirements> [<extra site directory> <modules to import first>]``.
# It prints one JSON line and exits 0 only when the adapter imports, every supported builtin loads and
# the graph validator reaches the packaged schema. The modules to import first are comma-separated:
# each one's outcome is reported under ``pre`` and none of them stops the adapter's own import.
PROBE = r'''
import importlib.abc
import importlib.machinery
import importlib.metadata as md
import json
import os
import re
import site
import sys
import sysconfig
from pathlib import Path

root = Path(sys.argv[1]).resolve()
reqs = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
extra_site = sys.argv[3] if len(sys.argv) > 3 else ""
pre_import = sys.argv[4] if len(sys.argv) > 4 else ""
if extra_site:
    # One more site directory, processed the way interpreter startup processes one: put on
    # sys.path with every .pth line in it executed, before any gate exists.
    site.addsitedir(extra_site)
NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


declared = set()
for line in reqs:
    line = line.split("#", 1)[0].strip()
    m = NAME.match(line)
    if m:
        declared.add(norm(m.group(1)))

allowed, todo = set(), list(declared)
while todo:
    d = todo.pop()
    if d in allowed:
        continue
    allowed.add(d)
    try:
        for r in md.requires(d) or ():
            if "extra ==" in r and 'extra == "binary"' not in r and 'extra == "crypto"' not in r:
                continue
            m = NAME.match(r)
            if m:
                todo.append(norm(m.group(1)))
    except md.PackageNotFoundError:
        pass

pkg_to_dists = md.packages_distributions()


def norm_path(path):
    return os.path.normcase(os.path.realpath(str(path))).rstrip("\\/")


local_dirs = [root / "server", root / "da", root / "platform"]
local_prefixes = [norm_path(d) + os.sep for d in local_dirs]
# The interpreter's own library directories: pure modules, extension modules, and on Windows the
# DLLs directory beside them.
std_roots = {norm_path(os.path.dirname(os.__file__))}
for key in ("stdlib", "platstdlib"):
    if sysconfig.get_path(key):
        std_roots.add(norm_path(sysconfig.get_path(key)))
if os.path.isdir(os.path.join(sys.base_exec_prefix, "DLLs")):
    std_roots.add(norm_path(os.path.join(sys.base_exec_prefix, "DLLs")))
std_prefixes = [r + os.sep for r in std_roots]
_recorded = {}


def interpreter_owned(origin):
    """A file inside the interpreter's own library directories and outside every site directory."""
    key = norm_path(origin)
    for prefix in std_prefixes:
        if key.startswith(prefix):
            parts = key[len(prefix):].replace("\\", "/").split("/")
            return "site-packages" not in parts and "dist-packages" not in parts
    return False


def recorded(dist):
    """A distribution's install directory and the relative paths its record lists (None: no record)."""
    if dist not in _recorded:
        try:
            found = md.distribution(dist)
            files, home = found.files, norm_path(found.locate_file(""))
        except md.PackageNotFoundError:
            files, home = (), ""
        _recorded[dist] = (home, None if files is None else {
            os.path.normcase(str(f)).replace("\\", "/") for f in files
        })
    return _recorded[dist]


def owners(origin):
    """The distributions whose record lists this file; None when no import root holds the file."""
    real = os.path.realpath(str(origin))
    key = os.path.normcase(real)
    bases = {os.path.realpath(p) for p in sys.path if p and os.path.isdir(p)}
    for base in sorted(bases, key=len, reverse=True):
        home = os.path.normcase(base).rstrip("\\/")
        prefix = home + os.sep
        if not key.startswith(prefix):
            continue
        rel = real[len(prefix):].replace("\\", "/")
        want = os.path.normcase(rel).replace("\\", "/")
        found = set()
        for dist in pkg_to_dists.get(rel.split("/")[0].split(".")[0], ()):
            installed, listed = recorded(dist)
            # A record describes files under the distribution's own install directory and nowhere
            # else. A distribution installed without a record is judged by the top-level name it provides.
            if installed == home and (listed is None or want in listed):
                found.add(norm(dist))
        return found
    return None


class Gate(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        # The interpreter's own order: built in, then frozen, then the import path.
        spec = (
            importlib.machinery.BuiltinImporter.find_spec(name, path)
            or importlib.machinery.FrozenImporter.find_spec(name, path)
            or importlib.machinery.PathFinder.find_spec(name, path)
        )
        if spec is None:
            # No later finder gets a turn: one that startup installed could resolve what no record lists.
            raise ModuleNotFoundError("No module named '" + name + "'", name=name)
        if spec.origin is None or not spec.has_location:
            # Built in, frozen, or a namespace package: no file of its own, and each module under a
            # namespace is judged by its own file.
            return spec
        key = norm_path(spec.origin)
        if any(key.startswith(prefix) for prefix in local_prefixes):
            return spec
        found = owners(spec.origin)
        if found:
            if found & allowed:
                return spec
        elif interpreter_owned(spec.origin):
            return spec
        raise ModuleNotFoundError("No module named '" + name + "' (not declared)", name=name)


sys.meta_path.insert(0, Gate())
# Whatever interpreter startup imported from outside its own library (a module a .pth line imported,
# a namespace a .pth line registered) never met the gate: drop it so its next import does.
for loaded, module in list(sys.modules.items()):
    spec = getattr(module, "__spec__", None)
    if loaded == "__main__" or getattr(spec, "origin", None) in ("built-in", "frozen"):
        continue
    file = getattr(module, "__file__", None)
    if file and interpreter_owned(file):
        continue
    del sys.modules[loaded]
sys.path.insert(0, str(root / "server"))
os.chdir(root / "server")

out = {"declared": sorted(declared), "pre": {}, "pre_file": {}}
for wanted in filter(None, pre_import.split(",")):
    try:
        __import__(wanted)
        out["pre"][wanted] = "imported"
        out["pre_file"][wanted] = getattr(sys.modules[wanted], "__file__", None)
    except ImportError as exc:
        out["pre"][wanted] = "refused: " + str(exc)
try:
    import solar_project_graph as adapter
    import solar_local_graph as local

    tools = sorted(adapter.SUPPORTED_TOOLS)
    for t in tools:
        local._load_builtin(t)
    out["tools"] = tools
    import solar_design_graph

    try:
        solar_design_graph.validate_graph({"graph_schema_version": solar_design_graph.SCHEMA_VERSION})
        out["validated"] = "accepted-empty"
    except solar_design_graph.GraphValidationError as exc:
        out["validated"] = "refused:" + str(exc).split(":")[0]
    out["schema"] = str(Path(solar_design_graph.SCHEMA_PATH).resolve())
    out["ok"] = True
except Exception as exc:  # noqa: BLE001 - the probe reports the first refusal
    out["ok"] = False
    out["error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(out))
sys.exit(0 if out.get("ok") else 1)
'''

# The adapter's own declared tool set, read without importing the adapter into this process.
_ADAPTER_SOURCE = (REPO / "server" / "solar_project_graph.py").read_text(encoding="utf-8")


def _probe_env() -> dict[str, str]:
    """A child environment with no cloud or APS credentials and no inherited import path."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AWS_", "APS_", "LEAF_", "PYTHON"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_probe(
    tmp_path: Path,
    root: Path,
    requirements: Path,
    extra_site: Path | None = None,
    pre_import: str = "",
) -> tuple[int, dict]:
    probe = tmp_path / "r4a_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    extra = [str(extra_site), pre_import] if extra_site is not None else []
    proc = subprocess.run(
        [sys.executable, "-B", str(probe), str(root), str(requirements), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_probe_env(),
        cwd=str(tmp_path),
        timeout=240,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"probe printed nothing; stderr: {proc.stderr[-2000:]}"
    return proc.returncode, json.loads(lines[-1])


_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_name(line: str) -> str:
    match = _REQUIREMENT_NAME.match(line.split("#", 1)[0])
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else ""


def _requirements_without(tmp_path: Path, name: str) -> Path:
    kept = [
        line
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if _requirement_name(line) != name
    ]
    path = tmp_path / f"requirements-without-{name}.txt"
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return path


def _logical_instructions(text: str) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    assert not pending, "Dockerfile ends with an incomplete instruction"
    return logical


def _local_copies(instructions: list[str]) -> list[tuple[str, str]]:
    """Every (source, destination) pair of a COPY that reads the build context."""
    pairs: list[tuple[str, str]] = []
    for instruction in instructions:
        if not instruction.upper().startswith("COPY "):
            continue
        tokens = shlex.split(instruction)
        if any(token.startswith("--from=") for token in tokens[1:]):
            continue
        values = [token for token in tokens[1:] if not token.startswith("--")]
        assert len(values) == 2, f"unexpected COPY shape: {instruction}"
        pairs.append((values[0], values[1]))
    return pairs


def test_sip_r4_runtime_dependencies(tmp_path):
    # The image installs exactly platform/requirements.txt. With only that closure importable, the
    # adapter, every supported builtin and the graph validator must load from the repository layout.
    rc, out = _run_probe(tmp_path, REPO, REQUIREMENTS)
    assert rc == 0 and out["ok"] is True, out
    assert "requests" in out["declared"] and "jsonschema" in out["declared"], out["declared"]
    assert out["tools"], out
    for tool in out["tools"]:
        assert f'"{tool}"' in _ADAPTER_SOURCE, tool
    # A refusal from the schema itself proves jsonschema imported and the packaged schema was read.
    assert out["validated"] == "refused:UNKNOWN_UNITS", out
    assert Path(out["schema"]) == (REPO / "contract" / "solar-design-graph.v1.schema.json").resolve()

    # Positive controls: the gate refuses a dependency the requirements file does not declare.
    rc, out = _run_probe(tmp_path, REPO, _requirements_without(tmp_path, "requests"))
    assert rc == 1 and out["ok"] is False, out
    assert "'requests' (not declared)" in out["error"], out
    rc, out = _run_probe(tmp_path, REPO, _requirements_without(tmp_path, "jsonschema"))
    assert rc == 1 and out["ok"] is False, out
    assert "'jsonschema' (not declared)" in out["error"], out


_PROBE_DIST = "leaf-probe-undeclared"
# A module of the interpreter's own library that startup never imports and no platform builds in.
_OWN_STANDARD_MODULE = "colorsys"
# A file in the site directory that no distribution's record lists.
_UNRECORDED_MODULE = "leafstray"
# A module with no file at all, served by an import hook the distribution installs at startup.
_HOOKED_MODULE = "leafhook"
_LEAFBOOT = '''\
import importlib.abc
import importlib.machinery
import sys

VALUE = 2


class _Hook(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        return importlib.machinery.ModuleSpec(name, self) if name == "leafhook" else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.VALUE = 6


sys.meta_path.append(_Hook())
'''


def _absent_standard_name() -> str:
    """A standard-library name this interpreter has no module for (another platform's module).

    The candidates are names nothing imports conditionally, so a stand-in file under one of them
    cannot change what the adapter's own dependencies do once the distribution is declared.
    """
    for name in ("winsound", "syslog"):
        if (
            name in sys.stdlib_module_names
            and name not in sys.builtin_module_names
            and importlib.util.find_spec(name) is None
        ):
            return name
    raise AssertionError("every candidate standard-library name is importable on this host")


def _undeclared_site(tmp_path: Path, standard_name: str) -> Path:
    """A site directory holding one installed distribution that the image does not declare.

    The distribution ships four modules, one for each way a host distribution can be reached without
    being judged by an import gate that trusts names:

    * ``leafboot``: a module one ``.pth`` line imports at startup, before any gate exists. Importing
      it also appends an import hook that serves ``leafhook``, a module with no file;
    * ``leafns.undeclared``: a package under a namespace a second ``.pth`` line registers at startup
      (a real example is protobuf's ``google`` namespace);
    * ``standard_name``: a file that takes a standard-library name this interpreter has no module for
      (a real example is pyreadline3's ``readline`` on Windows);
    * ``colorsys``: a file that takes the name of a module the interpreter does have. The site
      directory is searched after the interpreter's own library, so this copy is never the one
      imported, and the record that lists it must not make the interpreter's own file look third-party.

    Beside them sits ``leafstray.py``, a file the record does not list.
    """
    site_dir = tmp_path / "undeclared-site"
    package = site_dir / "leafns" / "undeclared"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site_dir / "leafboot.py").write_text(_LEAFBOOT, encoding="utf-8")
    (site_dir / f"{standard_name}.py").write_text("VALUE = 3\n", encoding="utf-8")
    (site_dir / f"{_OWN_STANDARD_MODULE}.py").write_text("VALUE = 4\n", encoding="utf-8")
    (site_dir / f"{_UNRECORDED_MODULE}.py").write_text("VALUE = 5\n", encoding="utf-8")
    info = site_dir / "leaf_probe_undeclared-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {_PROBE_DIST}\nVersion: 1.0\n", encoding="utf-8"
    )
    (info / "RECORD").write_text(
        "leafns/undeclared/__init__.py,,\n"
        "leafboot.py,,\n"
        f"{standard_name}.py,,\n"
        f"{_OWN_STANDARD_MODULE}.py,,\n"
        "leaf_probe_undeclared-1.0.dist-info/METADATA,,\n"
        "leaf_probe_undeclared-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    namespace = (site_dir / "leafns").as_posix()
    (site_dir / "leaf_probe.pth").write_text(
        "import leafboot\n"
        "import sys, types; m = sys.modules.setdefault('leafns', types.ModuleType('leafns')); "
        f"m.__path__ = [{namespace!r}]\n",
        encoding="utf-8",
    )
    return site_dir


def test_sip_r4_runtime_gate_judges_origin(tmp_path):
    standard_name = _absent_standard_name()
    site_dir = _undeclared_site(tmp_path, standard_name).resolve()
    shipped = ("leafns.undeclared", "leafboot", standard_name)
    first = ",".join((*shipped, _OWN_STANDARD_MODULE, _UNRECORDED_MODULE, _HOOKED_MODULE))

    def never_admitted(pre):
        # A file no record lists is admitted only from the interpreter's own library, and a module
        # only a startup-installed hook can find is never looked for: declared or not, both stay out.
        assert pre[_UNRECORDED_MODULE] == f"refused: No module named '{_UNRECORDED_MODULE}' (not declared)", pre
        assert pre[_HOOKED_MODULE] == f"refused: No module named '{_HOOKED_MODULE}'", pre

    # An installed distribution the requirements file does not declare is refused however its module
    # is reached: through a namespace startup registered, as a module startup already imported, or
    # under a standard-library name. The refusals do not stop the adapter, which needs none of them.
    rc, out = _run_probe(tmp_path, REPO, REQUIREMENTS, site_dir, first)
    assert rc == 0 and out["ok"] is True, out
    assert _PROBE_DIST not in out["declared"], out["declared"]
    for name in shipped:
        assert out["pre"][name] == f"refused: No module named '{name}' (not declared)", (name, out["pre"])
    # The interpreter's own module stays importable although an undeclared distribution's record
    # lists a file of the same name, and the file imported is the interpreter's.
    assert out["pre"][_OWN_STANDARD_MODULE] == "imported", out["pre"]
    assert site_dir not in Path(out["pre_file"][_OWN_STANDARD_MODULE]).resolve().parents, out["pre_file"]
    never_admitted(out["pre"])

    # The same distribution, declared: its three modules are admitted from its own files, so the
    # refusals above come from the missing declaration and not from the extra site directory.
    declared = tmp_path / "requirements-with-probe.txt"
    declared.write_text(
        REQUIREMENTS.read_text(encoding="utf-8").rstrip("\n") + f"\n{_PROBE_DIST}==1.0\n", encoding="utf-8"
    )
    rc, out = _run_probe(tmp_path, REPO, declared, site_dir, first)
    assert rc == 0 and out["ok"] is True, out
    assert _PROBE_DIST in out["declared"], out["declared"]
    assert out["validated"] == "refused:UNKNOWN_UNITS", out
    for name in shipped:
        assert out["pre"][name] == "imported", (name, out["pre"])
        assert site_dir in Path(out["pre_file"][name]).resolve().parents, (name, out["pre_file"])
    assert out["pre"][_OWN_STANDARD_MODULE] == "imported", out["pre"]
    assert site_dir not in Path(out["pre_file"][_OWN_STANDARD_MODULE]).resolve().parents, out["pre_file"]
    never_admitted(out["pre"])


def test_sip_r4_runtime_payload(tmp_path):
    # Lay out the image's /app from the Dockerfile's own build-context copies and run the probe there,
    # from /app/server, with the requirements file the image installs.
    instructions = _logical_instructions(DOCKERFILE.read_text(encoding="utf-8"))
    copies = _local_copies(instructions)
    assert ("da/", "/app/da/") in copies and ("contract/", "/app/contract/") in copies, copies
    assert ("server/", "/app/server/") in copies and ("platform/", "/app/platform/") in copies, copies

    # Both new roots land before the working directory moves and before the final survival guard.
    workdir = instructions.index("WORKDIR /app/server")
    last_run = max(index for index, item in enumerate(instructions) if item.upper().startswith("RUN "))
    for source in ("da/", "contract/"):
        index = next(i for i, item in enumerate(instructions) if item.startswith(f"COPY {source} "))
        assert index < workdir < last_run, (source, index, workdir, last_run)
    assert instructions[-1].startswith("CMD "), instructions[-1]

    app = tmp_path / "app"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for source, destination in copies:
        assert destination.startswith("/app/"), destination
        target = app / destination[len("/app/"):]
        origin = REPO / source.rstrip("/")
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=ignore, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if destination.endswith("/"):
                target = target / origin.name
            shutil.copy2(origin, target)

    assert (app / "da" / "store.py").is_file()
    assert (app / "contract" / "solar-design-graph.v1.schema.json").is_file()
    rc, out = _run_probe(tmp_path, app, app / "platform" / "requirements.txt")
    assert rc == 0 and out["ok"] is True, out
    assert out["validated"] == "refused:UNKNOWN_UNITS", out
    assert Path(out["schema"]) == (app / "contract" / "solar-design-graph.v1.schema.json").resolve()


def _load_manifest_module():
    spec = importlib.util.spec_from_file_location(
        "sip_r4_platform_release_manifest", REPO / "scripts" / "platform_release_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sip_r4_runtime_surface_coverage(monkeypatch):
    manifest = _load_manifest_module()
    declared = manifest.SURFACE_INPUTS["canonical-worker"]
    assert "da" in declared and "contract" in declared, declared

    # Read the Dockerfile from the working tree instead of a commit, so the row judges the change under test.
    def working_tree_git(repo_root, *args):
        assert args[0] == "show", args
        _, path = args[1].split(":", 1)
        return (REPO / path).read_text(encoding="utf-8")

    monkeypatch.setattr(manifest, "_git", working_tree_git)
    manifest._dockerfile_contract(REPO, "0" * 40, "canonical-worker")

    # Each new root is load-bearing: without it, the Dockerfile's copy is not fingerprinted.
    for root, source in (("da", "da/"), ("contract", "contract/")):
        monkeypatch.setitem(
            manifest.SURFACE_INPUTS,
            "canonical-worker",
            tuple(item for item in declared if item != root),
        )
        with pytest.raises(manifest.ContractError, match=f"not fingerprinted: {source}"):
            manifest._dockerfile_contract(REPO, "0" * 40, "canonical-worker")
    monkeypatch.setitem(manifest.SURFACE_INPUTS, "canonical-worker", declared)
