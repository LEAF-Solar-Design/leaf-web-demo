"""Pure validation adapter for the pinned cad-tool-package Linux runner."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
import re
import unicodedata


_VENDOR = Path(__file__).parent / "vendor" / "cad_tool_package"
_MAX_FILES = 64
_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BranchCheckResult:
    ok: bool
    failures: list[dict[str, str]]
    package_digest: str | None
    manifest: dict | None
    allowlist_sha256: str | None


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "_branch_tools_cad_tool_package_runner", _VENDOR / "runner.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load cad-tool-package runner")
    module = importlib.util.module_from_spec(spec)
    # exec_module can write __pycache__. Compile the loader's source instead,
    # keeping this check read-only without changing process-wide import flags.
    source = spec.loader.get_source(spec.name)
    if source is None:
        raise ImportError("cad-tool-package runner source missing")
    exec(compile(source, str(_VENDOR / "runner.py"), "exec"), module.__dict__)
    return module


def _failure(message: str) -> dict[str, str]:
    rule = re.match(r"^(V(?:[1-9]|1[0-7])):", message)
    path = re.search(r"(?:file |in |target )'([^']+)'", message)
    if path is None:
        path = re.search(r"'([^']+\.(?:csx|json))'", message)
    if path is None:
        path = re.search(r"\b(tool\.json|main\.csx)\b", message)
    return {
        "rule": rule.group(1) if rule else "internal",
        "path": path.group(1) if path else "",
        "message": message,
    }


# R2-11 pick inputs: twin of pickDiagnostics in harness branchPackageSchema.ts, which mirrors
# PickSpec in Branch2025 PackageValidator.cs (Branch2025 #746).
PICK_MIN_HOST_API = 2
MAX_PICK_INPUTS = 8
PICK_DXF_NAMES = (
    "LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE", "INSERT", "TEXT", "MTEXT", "HATCH", "POINT",
    "SPLINE", "ELLIPSE", "SOLID", "3DFACE", "3DSOLID", "REGION", "RAY", "XLINE", "DIMENSION", "LEADER",
    "MULTILEADER",
)
_PICK_KEYS = ("prompt", "dxf", "closed", "implied")
_REFUSED_PROMPT_CATEGORIES = frozenset(("Cc", "Cf", "Zl", "Zp", "Cs", "Cn", "Co"))


def _printable_prompt(value) -> bool:
    # PickSpec.PrintablePrompt counts UTF-16 code units.
    return (isinstance(value, str) and 1 <= len(value.encode("utf-16-le", "surrogatepass")) // 2 <= 120
            and value.strip() != ""
            and not any(unicodedata.category(c) in _REFUSED_PROMPT_CATEGORIES for c in value))


def pick_failures(params, hosts=None) -> list[dict[str, str]]:
    """Check each x-leaf-pick on params.properties against the contract shape (rule V7)."""
    if not isinstance(params, dict) or not isinstance(params.get("properties"), dict):
        return []
    failures = []
    picks = 0
    for name, prop in params["properties"].items():
        if not isinstance(prop, dict) or "x-leaf-pick" not in prop:
            continue
        picks += 1
        path = f"params.properties.{name}.x-leaf-pick"

        def fail(message):
            failures.append({"rule": "V7", "path": path, "message": f"V7: {path}: {message}"})

        pick = prop["x-leaf-pick"]
        if prop.get("type") != "string":
            fail('x-leaf-pick is only valid on a "type": "string" property')
        if not isinstance(pick, dict):
            fail("expected an object with prompt and dxf")
            continue
        for key in pick:
            if key not in _PICK_KEYS:
                fail(f"unknown key {key}; allowed: {', '.join(_PICK_KEYS)}")
        if not _printable_prompt(pick.get("prompt")):
            fail("prompt must be 1..120 printable characters")
        dxf = pick.get("dxf")
        if (not isinstance(dxf, list) or not 1 <= len(dxf) <= 16
                or not all(isinstance(n, str) and n in PICK_DXF_NAMES for n in dxf) or len(set(dxf)) != len(dxf)):
            fail(f"dxf must list 1..16 distinct names from: {', '.join(PICK_DXF_NAMES)}")
        for key in ("closed", "implied"):
            if key in pick and not isinstance(pick[key], bool):
                fail(f"{key} must be a boolean")
    if picks > MAX_PICK_INPUTS:
        failures.append({"rule": "V7", "path": "params.properties",
                         "message": f"V7: at most {MAX_PICK_INPUTS} x-leaf-pick inputs"})
    host_api = hosts.get("min_host_api") if isinstance(hosts, dict) else None
    if picks and not (type(host_api) is int and host_api >= PICK_MIN_HOST_API):
        failures.append({"rule": "V10", "path": "hosts",
                         "message": f"V10: x-leaf-pick inputs need hosts.min_host_api >= {PICK_MIN_HOST_API}"})
    return failures


def check_branch_package(files: Mapping[str, bytes], name: str) -> BranchCheckResult:
    """Check in-memory package bytes without executing any package scripts.

    Metadata stays None until available; exceptions always produce an internal
    failure, including failures to load the vendored runner or allowlist.
    """
    failures = []
    digest = None
    manifest = None
    allowlist_sha256 = None
    try:
        if len(files) > _MAX_FILES:
            failures.append({"rule": "cap_files", "path": "",
                             "message": "package exceeds 64 files"})
        if sum(len(data) for data in files.values()) > _MAX_BYTES:
            failures.append({"rule": "cap_bytes", "path": "",
                             "message": "package exceeds 4 MiB"})
        if failures:
            return BranchCheckResult(False, failures, digest, manifest, allowlist_sha256)
        package = dict(files)
        if not isinstance(name, str) or any(
            not isinstance(path, str) or not isinstance(data, bytes)
            for path, data in package.items()
        ):
            raise TypeError("expected a string name and a mapping of paths to bytes")
        allowlist_sha256 = hashlib.sha256(
            (_VENDOR / "api-allowlist.json").read_bytes()
        ).hexdigest()
        runner = _load_runner()
        messages = []
        try:
            manifest = runner.validate(package, name, messages)
        finally:
            failures.extend(_failure(message) for message in messages)
        digest = runner.package_digest(package)
        if isinstance(manifest, dict):
            failures.extend(pick_failures(manifest.get("params"), manifest.get("hosts")))
        if not isinstance(manifest, dict):
            manifest = None
            if not failures:
                failures.append(_failure("runner returned no manifest"))
    except Exception as exc:
        failures.append(_failure(f"internal: {type(exc).__name__}: {exc}"))
    return BranchCheckResult(not failures, failures, digest, manifest, allowlist_sha256)
