"""Small, bounded source-surface extractors and local contract comparison."""

from dataclasses import dataclass, field
from pathlib import Path
import ast
import re
import subprocess
import tempfile
from typing import Callable
from urllib.parse import urlsplit

try:
    from . import gitio
    from .manifest import match_path
except ImportError:
    import gitio
    from manifest import match_path

MAX_FILES = 500
MAX_BYTES = 2 * 1024 * 1024


@dataclass
class Surface:
    endpoints: list[tuple[str, str]] = field(default_factory=list)
    fields: dict[str, set[str]] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    strict: bool = False


Extractor = Callable[[dict[str, str]], Surface]


def normalize(path):
    path = re.sub(r"\$\{[^}]*\}", ":param", path)
    path = re.sub(r"\[(?:\[)?(?:\.\.\.)?[^/\]]+\](?:\])?", ":param", path)
    path = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", ":param", path)
    return path.rstrip("/") or "/"


def ts_fetch_paths(files):
    endpoints = set()
    pattern = re.compile(r"\bfetch\s*\(\s*([\"'`])(/[^\n]*?)\1")
    for _, text in sorted(files.items()):
        for call in pattern.finditer(text):
            tail = text[call.end():call.end() + 400]
            # Limit method discovery to this call's options, never a later fetch.
            tail = re.split(r"\bfetch\s*\(", tail, maxsplit=1)[0]
            tail = tail.split(");", 1)[0]
            method = re.search(r"\bmethod\s*:\s*([\"'])([A-Za-z]+)\1", tail)
            endpoints.add((method[2].upper() if method else "*", normalize(call[2])))
    return Surface(sorted(endpoints))


def next_app_routes(files):
    endpoints = set()
    for path, text in sorted(files.items()):
        route = re.search(r"(?:^|/)app/api/(?:(.*)/)?route\.(?:ts|js)$", path)
        if route is None:
            continue
        endpoint = normalize("/api/" + (route[1] or ""))
        methods = re.findall(r"\bexport\s+(?:async\s+function\s+|const\s+)(GET|POST|PUT|PATCH|DELETE)\b", text)
        endpoints.update((method, endpoint) for method in methods or ["*"])
    return Surface(sorted(endpoints))


def swift_path_literals(files):
    endpoints = set()
    for _, text in sorted(files.items()):
        root = re.search(r'\bstatic\s+let\s+root\s*=\s*"([^"]+)"', text)
        prefix = root[1] if root else ""
        for call in re.finditer(r'\brequest\s*\(\s*path:\s*"([^"]+)"', text):
            path = call[1]
            tail = text[call.end():]
            parameter = re.match(r'\s*\+\s*(?:component\s*\([^)\n]*\)|[A-Za-z_]\w*)', tail)
            if parameter:
                path = path.rstrip("/") + "/:param"
                suffix = re.match(r'\s*\+\s*"([^"]+)"', tail[parameter.end():])
                if suffix:
                    path += suffix[1]
            endpoints.add(("*", normalize(path if path.startswith("/api/") else prefix + path)))
        for literal in re.finditer(r'"(/api/[^"]*)"', text):
            # The root declaration is a prefix, not a request to that endpoint.
            if root and root.start() <= literal.start() < root.end():
                continue
            endpoints.add(("*", normalize(literal[1])))
    return Surface(sorted(endpoints))


def python_pydantic_fields(files):
    endpoints, fields = set(), {}
    for _, text in sorted(files.items()):
        tree = ast.parse(text.lstrip("\ufeff"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.match(r"https?://[^/\s]+/", node.value):
                    endpoints.add(("*", normalize(urlsplit(node.value).path)))
            if isinstance(node, ast.ClassDef) and node.name.endswith("Request"):
                bases = [base.id if isinstance(base, ast.Name) else base.attr
                         if isinstance(base, ast.Attribute) else "" for base in node.bases]
                if any(name.endswith("Model") for name in bases):
                    fields.setdefault(node.name, set()).update(
                        item.target.id for item in node.body
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name))
    return Surface(sorted(endpoints), fields)


def csharp_json_properties(files):
    endpoints, fields = set(), {}
    found_request = False
    for _, text in sorted(files.items()):
        for literal in re.finditer(r'"([^"\r\n]*://[^"\r\n]*)"', text):
            endpoints.add(("*", normalize(urlsplit(literal[1]).path)))
        declaration = re.search(r'\bclass\s+(\w*Request)\b[^{]*\{', text, re.IGNORECASE)
        if found_request or declaration is None:
            continue
        found_request = True
        names = fields.setdefault(declaration[1], set())
        depth, attribute = 1, None
        # Only direct properties of this request, never nested DTO properties.
        for line in text[declaration.end():].splitlines():
            if depth == 1:
                rename = re.search(r'\[JsonProperty\s*\(\s*"([^"]+)"', line)
                if rename:
                    attribute = rename[1]
                prop = re.search(r'\bpublic\s+[\w.<>,?\[\]]+\s+(\w+)\s*\{\s*get;\s*set;\s*\}', line)
                if prop:
                    names.add(attribute or prop[1])
                    attribute = None
                elif line.strip() and not line.strip().startswith("["):
                    attribute = None
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
    return Surface(sorted(endpoints), fields)


def fastapi_request_keys(files):
    endpoints, keys, required = set(), set(), set()
    key = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
    for _, text in sorted(files.items()):
        for route in re.finditer(r'@(?:app|router)\.(get|post|put|patch|delete|head|options)\s*\(\s*(["\'])(/[^"\']*)\2', text):
            endpoints.add((route[1].upper(), normalize(route[3])))
        required.update(re.findall(r'["\']Missing (' + key + r')["\']', text))
        keys.update(re.findall(r'\b(?:body|payload|data)\.get\s*\(\s*["\'](' + key + r')["\']', text))
        keys.update(re.findall(r'\bbody\s*\[\s*["\'](' + key + r')["\']\s*\]', text))
    return Surface(sorted(endpoints), {"request": keys | required}, required)


EXTRACTORS: dict[str, Extractor] = {
    "ts-fetch-paths": ts_fetch_paths, "next-app-routes": next_app_routes,
    "swift-path-literals": swift_path_literals, "python-pydantic-fields": python_pydantic_fields,
    "csharp-json-properties": csharp_json_properties, "fastapi-request-keys": fastapi_request_keys,
}


def compare(producer, consumer):
    missing = []
    for method, path in sorted(set(consumer.endpoints)):
        if not any(normalize(path) == normalize(other_path)
                   and (method == "*" or other_method == "*" or method == other_method)
                   for other_method, other_path in producer.endpoints):
            missing.append(f"{method} {normalize(path)}")
    known = set().union(*producer.fields.values())
    wanted = set().union(*consumer.fields.values())
    missing.extend(f"missing {name}" for name in sorted(producer.required - wanted))
    extra = sorted(wanted - known)
    detail = "\n".join([*missing, *(f"extra {name}" for name in extra)])
    return ("mismatch" if missing or (producer.strict and extra) else "match", detail)


def git_text(repo, *args, limit=MAX_BYTES):
    # File-backed subprocess output keeps hostile blobs out of unbounded memory reads.
    argv = ["git", "-C", str(repo), *args]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, stdout=output, stderr=errors, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise gitio.GitError(argv, str(exc)) from exc
        if result.returncode:
            errors.seek(0)
            raise gitio.GitError(argv, errors.read(300).decode("utf-8", errors="replace"))
        output.seek(0)
        raw = output.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("git output exceeds byte limit")
    return raw.decode("utf-8")


def _files(endpoint, workdir, head_sha, index_lookup):
    revision = endpoint["revision"]
    if revision == "unresolved":
        raise ValueError("revision unresolved")
    extractor_id = endpoint.get("extractor")
    if extractor_id not in EXTRACTORS:
        raise ValueError(f"unknown extractor: {extractor_id}")
    local = endpoint["repo"] == "self"
    repo = Path(workdir)
    if not local:
        entry = index_lookup(endpoint["repo"])
        if not entry or not entry.get("path"):
            raise ValueError(f"no index entry for {endpoint['repo']}")
        repo = Path(entry["path"])
    worktree = local and revision == "HEAD" and head_sha.startswith("worktree")
    if local and revision == "HEAD":
        revision = "HEAD" if worktree else head_sha
    sha = gitio.rev_parse(repo, revision)
    paths = gitio.ls_tree(repo, sha)
    if worktree:
        listed = git_text(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z", limit=8 * MAX_BYTES)
        paths = sorted(set(paths) | set(filter(None, listed.split("\0"))))
    paths = [path for path in paths if match_path(endpoint["paths"], path)]
    if len(paths) > MAX_FILES:
        raise ValueError("contract exceeds 500 files")
    files, total = {}, 0
    for path in paths:
        if worktree:
            candidate = repo / path
            if not candidate.exists():
                continue
            if not candidate.resolve().is_relative_to(repo.resolve()):
                raise ValueError("contract path escapes workdir")
            with candidate.open("rb") as stream:
                raw = stream.read(MAX_BYTES - total + 1)
            text = raw.decode("utf-8")
        else:
            text = git_text(repo, "show", f"{sha}:{path}", limit=MAX_BYTES - total)
        total += len(text.encode("utf-8"))
        if total > MAX_BYTES:
            raise ValueError("contract exceeds 2 MiB")
        files[path] = text
    return EXTRACTORS[extractor_id](files), sha


def contract_check(contract, consumer, workdir, head_sha, index_lookup):
    producer_sha = consumer_sha = "unknown"
    try:
        producer, producer_sha = _files(contract["producer"], workdir, head_sha, index_lookup)
        used, consumer_sha = _files(consumer, workdir, head_sha, index_lookup)
        status, detail = compare(producer, used)
        return status, detail, producer_sha, consumer_sha
    except (OSError, ValueError, SyntaxError, gitio.GitError, RecursionError) as exc:
        return "unreachable", " ".join(str(exc).splitlines())[:900], producer_sha, consumer_sha
