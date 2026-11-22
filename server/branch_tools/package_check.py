"""Pure validation adapter for the pinned cad-tool-package Linux runner."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
import re


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
        if not isinstance(manifest, dict):
            manifest = None
            if not failures:
                failures.append(_failure("runner returned no manifest"))
    except Exception as exc:
        failures.append(_failure(f"internal: {type(exc).__name__}: {exc}"))
    return BranchCheckResult(not failures, failures, digest, manifest, allowlist_sha256)
