"""Process-local Python read observation, never a security or native-child sandbox."""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import quote


def encoded_suite(suite_id):
    # Quote dots too, so '.' and '..' cannot become directory traversal.
    return quote(str(suite_id), safe="").replace(".", "%2E") or "%00"


def repo_relative_path(root, raw, base=None):
    """Return a lexical POSIX identity without changing repository path case."""
    raw = os.fsdecode(raw)
    raw.encode("utf-8", "strict")
    if "\x00" in raw:
        raise ValueError("nul")
    root = Path(root)
    path = Path(raw)
    if not path.is_absolute():
        path = Path(base) / path if base is not None else root / path
    return Path(os.path.abspath(path)).relative_to(root).as_posix()


def repo_nodeid(root, nodeid, base=None, path=None):
    """Normalize only the path; preserve test names and parameter IDs verbatim."""
    module, separator, suffix = nodeid.partition("::")
    return repo_relative_path(root, path if path is not None else module, base) + separator + suffix


class Capture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.local = threading.local()
        self.lock = threading.RLock()
        self.owner = threading.get_ident()
        self.active = None
        self.process_reads = set()
        self.module_reads = {}
        self.incomplete = set()
        self.module_incomplete = {}
        self.external = set()
        self.test_ids = {}
        self.enabled = False

    @contextmanager
    def suspended(self):
        previous = getattr(self.local, "busy", False)
        self.local.busy = True
        try:
            yield
        finally:
            self.local.busy = previous

    def install(self):
        if not self.enabled:
            self.enabled = True
            sys.addaudithook(self.audit)
        return self

    def close(self):
        # CPython cannot remove an audit hook. Make this instance inert.
        self.enabled = False

    def scope(self):
        if threading.get_ident() != self.owner or getattr(self.local, "shared", False):
            return None
        return self.active

    def mark_incomplete(self, reason, module=None):
        with self.lock:
            if module is None:
                self.incomplete.add(reason)
            else:
                self.module_incomplete.setdefault(module, set()).add(reason)

    def paths(self, raw):
        if isinstance(raw, int) or raw is None:
            self.mark_incomplete("unresolved_file_descriptor")
            return []
        try:
            raw = os.fsdecode(raw)
            raw.encode("utf-8", "strict")
            if "\x00" in raw:
                raise ValueError("nul")
            path = Path(raw)
            relative = not path.is_absolute()
            lexical = Path(os.path.abspath(path))
            resolved = lexical.resolve(strict=False)
        except (TypeError, ValueError, OSError, UnicodeError, RuntimeError):
            self.mark_incomplete("path_normalization_error")
            return []
        results = []
        for candidate in (lexical, resolved):
            try:
                rel = repo_relative_path(self.root, candidate)
            except ValueError:
                self.external.add("python-environment")
                if relative:
                    self.mark_incomplete("path_escape")
                continue
            if rel not in results:
                results.append(rel)
        if lexical != resolved and len(results) < 2:
            self.mark_incomplete("symlink_escape")
        return results

    def record_path(self, raw, kind):
        module = self.scope()
        if threading.get_ident() != self.owner:
            self.mark_incomplete("background_thread_shared")
        normalized = self.paths(raw)
        with self.lock:
            bucket = (self.module_reads.setdefault(module, set())
                      if module else self.process_reads)
            for path in normalized:
                bucket.add((kind, path))
            # Retain both bytecode and source identities; never discard data files.
            if isinstance(raw, (str, bytes)) and os.fsdecode(raw).endswith(".pyc"):
                try:
                    source = importlib.util.source_from_cache(os.fsdecode(raw))
                except ValueError:
                    source = os.fsdecode(raw)[:-1]
                for path in self.paths(source):
                    bucket.add(("import", path))

    def audit(self, event, args):
        if not self.enabled or getattr(self.local, "busy", False):
            return
        with self.suspended():
            try:
                if event == "open":
                    self.record_path(args[0], "open")
                elif event == "import" and len(args) > 1 and args[1]:
                    self.record_path(args[1], "import")
                elif event in ("os.listdir", "os.scandir"):
                    self.record_path(args[0] if args and args[0] is not None else os.getcwd(), "enumerate")
                elif event == "subprocess.Popen":
                    # Deliberately do not inspect argv, executable, cwd or env.
                    self.mark_incomplete("child-requires-trace", self.scope())
                elif event in ("os.system", "os.fork", "os.posix_spawn", "os.exec", "os.spawn"):
                    self.mark_incomplete("child-requires-trace", self.scope())
            except Exception:
                self.mark_incomplete("audit_record_error", self.scope())

    def snapshot_modules(self):
        with self.suspended():
            try:
                modules = list(sys.modules.values())
                for module in modules:
                    if module is None:
                        continue
                    attrs = vars(module)
                    origin = attrs.get("__file__")
                    spec = attrs.get("__spec__")
                    if not origin and spec is not None:
                        origin = getattr(spec, "origin", None)
                    if isinstance(origin, str) and origin not in ("built-in", "frozen"):
                        self.record_path(origin, "import")
                    loader = attrs.get("__loader__")
                    archive = getattr(loader, "archive", None)
                    if isinstance(archive, str):
                        self.record_path(archive, "archive")
            except Exception:
                self.mark_incomplete("module_snapshot_error", self.scope())

    def begin_module(self, module, nodeid=None):
        module = repo_relative_path(self.root, module)
        if nodeid:
            nodeid = repo_nodeid(self.root, nodeid)
        if threading.get_ident() != self.owner or self.active is not None:
            self.mark_incomplete("ambiguous_module_scope")
        self.active = module
        if nodeid:
            self.test_ids.setdefault(module, set()).add(nodeid)
        self.snapshot_modules()

    def end_module(self):
        self.snapshot_modules()
        self.active = None

    @contextmanager
    def shared_scope(self):
        previous = getattr(self.local, "shared", False)
        self.local.shared = True
        try:
            yield
        finally:
            self.local.shared = previous

    def document(self, suite_id, module=None, complete=False, **metadata):
        with self.suspended(), self.lock:
            module = repo_relative_path(self.root, module or suite_id)
            records = set(self.process_reads)
            records.update(self.module_reads.get(module, set()))
            reasons = self.incomplete | self.module_incomplete.get(module, set())
            reasons = set(reasons)
            if not complete:
                reasons.add("missing_completion")
            required = ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")
            if any(not metadata.get(key) for key in required):
                reasons.add("missing_provenance")
            # Native extensions are outside audit coverage. The producer must
            # explicitly restrict admission to supported Python-only execution.
            if metadata.pop("python_only", False) is not True:
                reasons.add("native_reads_unverified")
            reads = [{"path": path, "kind": kind} for kind, path in sorted(records)
                     if kind != "enumerate"]
            directories = [{"path": path, "members": sorted({
                item["path"] for item in reads
                if str(Path(item["path"]).parent).replace("\\", "/") == path})}
                for kind, path in sorted(records) if kind == "enumerate"]
            # Members are only observed leaves, never an eligibility glob.
            allowed = required + ("attempt", "worker", "outcomes_ref")
            doc = {key: metadata[key] for key in allowed if key in metadata}
            doc.update(schema="leaf.ci.readset.v1", suite_id=suite_id,
                       nodeids=sorted(self.test_ids.get(module, set())),
                       test_ids=sorted(suite_id + "::" + nodeid
                                       for nodeid in self.test_ids.get(module, set())),
                       reads=reads, directory_reads=directories, generated_inputs=[],
                       external_dependency_classes=sorted(self.external),
                       children_complete="child-requires-trace" not in reasons,
                       capture_complete=not reasons, incomplete_reasons=sorted(reasons),
                       completion_marker=bool(complete))
            return doc


def write_readset(path, capture=None, suite_id=None, module=None, complete=False, **metadata):
    """Atomically flush a capture using a descriptor opened with tracing suspended."""
    if capture is None:
        raise ValueError("capture is required")
    with capture.suspended():
        document = capture.document(suite_id, module, complete, **metadata)
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return document


def new_read_edges(readset, known_paths, map_sha):
    """Produce revocation *requests*. Only the trusted collector can revoke a map."""
    known = set(known_paths)
    observed = {row["path"] for row in readset.get("reads", [])}
    return [{"schema": "leaf.ci.selection-miss.v1", "kind": "new_read_edge",
             "run_id": readset.get("run_id"), "map_sha": map_sha,
             "suite_id": readset["suite_id"], "path": path,
             "readset_sha256": hashlib.sha256(json.dumps(
                 readset, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
             "revocation_requested": True}
            for path in sorted(observed - known)]
