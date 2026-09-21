"""Host discovery for change-impact.

sync(home=None) returns a newly persisted index. doctor(home=None) returns an
inspection dict. lookup(repository, home=None) returns an entry or None for clean
absence, and raises IndexError on unreadable state. IMPACT_HOME isolates all host
sources, including the sibling <home>-dispatch directory.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

try:
    from . import gitio
    from .manifest import ManifestError, REPOSITORY_PATTERN, load_manifest, manifest_digest, read_text
except ImportError:
    import gitio
    from manifest import ManifestError, REPOSITORY_PATTERN, load_manifest, manifest_digest, read_text


class IndexError(ValueError):
    """The host index cannot be read or has an invalid shape."""


def home_path(home=None):
    return Path(os.path.expanduser(str(home) if home is not None else os.environ.get("IMPACT_HOME", "~/.claude"))).resolve()


def index_path(home=None):
    return home_path(home) / "state" / "impact-index.json"


def _optional(path, yaml_file=False):
    # Optional discovery inputs are bounded to 1 MiB; malformed inputs are skipped.
    try:
        text = read_text(path)
        return yaml.safe_load(text) if yaml_file else json.loads(text)
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        return None


def _record(path):
    if not isinstance(path, str) or not path or not Path(path).is_dir():
        return None
    top = gitio.toplevel(path)
    if top is None:
        return None
    manifest_path = Path(top) / "ASPECTS.yaml"
    if not manifest_path.is_file():
        return None
    try:
        loaded = load_manifest(manifest_path)
    except (ManifestError, OSError, ValueError, RecursionError):
        return None
    repository = loaded["repository"]
    if repository == "local":
        repository = f"local:{loaded['project_id']}"
    return repository, {"path": top, "manifest": str(manifest_path), "digest": manifest_digest(loaded), "project_id": loaded["project_id"]}


def discover(home=None):
    # Scan at most 5,000 status files; repo-map entries override pair-run entries.
    root = home_path(home)
    repos = {}
    seen = set()
    pair_runs = root / "pair-runs"
    if pair_runs.is_dir():
        scanned = 0
        for run in sorted(pair_runs.iterdir(), key=lambda value: value.name):
            if run.name.startswith("_") or not run.is_dir():
                continue
            status = run / "status.json"
            if not status.is_file():
                continue
            if scanned == 5000:
                break
            scanned += 1
            data = _optional(status)
            if not isinstance(data, dict):
                continue
            workdir = data.get("workdir")
            if workdir is None and isinstance(data.get("spec"), dict):
                workdir = data["spec"].get("workdir")
            if not isinstance(workdir, str) or workdir in seen:
                continue
            seen.add(workdir)
            record = _record(workdir)
            if record:
                repos[record[0]] = record[1]
    repo_map = _optional(root / "ASPECTS.yaml", yaml_file=True)
    if isinstance(repo_map, dict) and isinstance(repo_map.get("repos"), dict):
        for name, path in sorted(repo_map["repos"].items(), key=lambda item: str(item[0])):
            if not isinstance(name, str) or not isinstance(path, str):
                continue
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            record = _record(str(candidate))
            if record:
                repos[record[0]] = record[1]
    return dict(sorted(repos.items()))


def _registry(root):
    data = _optional(root / "ci-registry.json")
    if isinstance(data, dict):
        for key in ("repos", "repositories"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
    entries = data.keys() if isinstance(data, dict) else data if isinstance(data, list) else []
    names = set()
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("repository", entry.get("repo", entry.get("name")))
        if isinstance(entry, str) and re.fullmatch(REPOSITORY_PATTERN, entry):
            names.add(entry)
    return sorted(names)


def _hosts(root):
    data = _optional(Path(str(root) + "-dispatch") / "devices.yaml", yaml_file=True)
    if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
        return []
    hosts = []
    for name, value in data["devices"].items():
        if not isinstance(name, str):
            continue
        host = {"name": name}
        if isinstance(value, dict) and isinstance(value.get("role"), str):
            host["role"] = value["role"]
        hosts.append(host)
    return sorted(hosts, key=lambda item: item["name"])


def sync(home=None):
    root = home_path(home)
    data = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "repos": discover(root), "registry": _registry(root), "hosts": _hosts(root)}
    destination = index_path(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise IndexError("index exceeds 1 MiB limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent, prefix=".impact-index-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return data


def read_index(home=None):
    # Index reads are bounded to 1 MiB and reject malformed repository entries.
    try:
        data = json.loads(read_text(index_path(home)))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1 or not isinstance(data.get("repos"), dict):
            raise IndexError("invalid index structure")
        for name, record in data["repos"].items():
            if not isinstance(name, str) or not isinstance(record, dict):
                raise IndexError("invalid index repository entry")
            for field in ("path", "manifest", "digest", "project_id"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise IndexError(f"invalid index repository {name}: {field}")
        if not isinstance(data.get("registry", []), list) or any(not isinstance(value, str) for value in data.get("registry", [])):
            raise IndexError("invalid index registry")
        return data
    except (OSError, ValueError, RecursionError) as exc:
        raise IndexError(f"{index_path(home)}: {exc}") from exc


def lookup(repository, home=None):
    return read_index(home)["repos"].get(repository)


def doctor(home=None):
    root = home_path(home)
    discovered = discover(root)
    registry = _registry(root)
    try:
        data = read_index(root)
    except IndexError:
        return {"ok": False, "missing_index": True, "unreachable": sorted(discovered), "no_manifest": sorted(set(registry) - discovered.keys())}
    unreachable = []
    for repository, record in discovered.items():
        entry = data["repos"].get(repository)
        if entry is None or entry["path"] != record["path"] or entry["manifest"] != record["manifest"] or not Path(entry["manifest"]).is_file():
            unreachable.append(repository)
    registry = sorted(set(registry) | set(data.get("registry", [])))
    reachable = {name for name, entry in data["repos"].items() if Path(entry["manifest"]).is_file()}
    return {"ok": not unreachable, "missing_index": False, "unreachable": sorted(unreachable), "no_manifest": sorted(set(registry) - (reachable | discovered.keys()))}
