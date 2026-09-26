"""Trusted pytest plugin. No import of pytest is needed to declare its hooks.

Load with -p pytest_selection from an extracted trusted directory. The adapter
supplies --leaf-selection, --leaf-catalog, --leaf-repo and --leaf-output.
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import sys

try:
    from . import select_tests as selection
    from . import trace_reads
except ImportError:
    import select_tests as selection
    import trace_reads


def hook(**options):
    # Equivalent to pytest.hookimpl; pluggy reads this public marker attribute.
    def decorate(function):
        function.pytest_impl = options
        return function
    return decorate


def pytest_addoption(parser):
    group = parser.getgroup("leaf-selection")
    for name in ("selection", "catalog", "repo", "output"):
        group.addoption("--leaf-" + name, default=None)


def pytest_configure(config):
    if config.getoption("leaf_output"):
        plugin = SelectionPlugin(config)
        config.pluginmanager.register(plugin, "leaf-selection-state")


def validate_collection_or_choose_full(collected, decision, catalog):
    """Pure validation before any deselection. Collection errors keep their verdict."""
    chosen = dict(decision)
    try:
        entries, fingerprint = selection.catalog_info(catalog)
        if (decision.get("schema") != selection.DECISION_SCHEMA or
                decision.get("catalog_sha256") != fingerprint or catalog.get("kind") != "pytest"):
            raise selection.InvalidInput("catalog_change")
        expected = sorted(tid for row in entries.values() for tid in row["test_ids"])
        actual = sorted(selection.strings(list(collected), "collection_drift"))
        if actual != expected or decision.get("collection_ids_sha256") != selection.collection_digest(actual):
            raise selection.InvalidInput("collection_drift")
        if decision.get("apply_filter") is True:
            if (decision.get("execution_mode") != "selected" or
                    decision.get("selection_mode") != "selected" or
                    decision.get("assigned_arm") != "selected"):
                raise selection.InvalidInput("invalid_decision")
            selected = set(selection.strings(decision.get("expanded_suite_ids")))
            mandatory = set(selection.strings(decision.get("mandatory_suite_ids")))
            if not selected or not mandatory <= selected or not selected <= set(entries):
                raise selection.InvalidInput("incomplete_selection_closure")
            modules = sorted({selection.repo_path(entries[sid]["module"]) for sid in selected})
            if modules != decision.get("selected_modules"):
                raise selection.InvalidInput("module_identity_drift")
            for row in entries.values():
                module = selection.repo_path(row["module"])
                if any(tid.split("::", 1)[0] != module for tid in row["test_ids"]):
                    raise selection.InvalidInput("module_identity_drift")
        elif decision.get("execution_mode") != "full":
            raise selection.InvalidInput("invalid_decision")
        chosen["collection_complete"] = True
    except (selection.InvalidInput, KeyError, TypeError, ValueError) as exc:
        selection.full(chosen, str(exc) if isinstance(exc, selection.InvalidInput) else "invalid_collection")
    return chosen


def shadow_evidence(attempts, selected_test_ids, assigned_arm, execution_mode):
    """Keep first failures even when a later retry passes; do not alter verdicts."""
    failed = sorted({row["nodeid"] for row in attempts
                     if row.get("attempt") == 1 and row.get("outcome") in ("failed", "rerun")})
    selected = sorted(set(selected_test_ids))
    phases = {}
    for row in attempts:
        phases.setdefault((row.get("nodeid"), row.get("attempt")), set()).add(row.get("phase"))
    reporting_complete = (bool(phases) and all(
        nodeid and type(attempt) is int and attempt > 0 and
        {"setup", "teardown"} <= values <= {"setup", "call", "teardown"}
        for (nodeid, attempt), values in phases.items()))
    return {"full_failed_ids": failed, "selected_test_ids": selected,
            "contained": set(failed) <= set(selected), "assigned_arm": assigned_arm,
            "execution_mode": execution_mode, "first_attempt": True,
            "test_id_reporting_complete": reporting_complete}


class SelectionPlugin:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.getoption("leaf_repo") or config.rootpath).resolve()
        self.output = Path(config.getoption("leaf_output")).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        workerinput = getattr(config, "workerinput", {})
        self.worker = trace_reads.encoded_suite(workerinput.get("workerid", "main"))
        self.shard = self.worker + "-" + str(os.getpid())
        self.collection = []
        self.nodeids = {}
        self.executable_ids = []
        self.quarantine = set()
        self.selection_omitted = []
        self.filtering = False
        self.attempts = []
        self.attempt_numbers = {}
        self.collection_errors = []
        self.worker_collections = {}
        self.worker_completions = {}
        self.worker_started = set()
        self.worker_errors = []
        self.decision = {"schema": selection.DECISION_SCHEMA, "selection_mode": "full",
                         "execution_mode": "full", "apply_filter": False, "reasons": []}
        self.catalog = {}
        try:
            self.decision, _ = selection.load_json(config.getoption("leaf_selection"))
            self.catalog, _ = selection.load_json(config.getoption("leaf_catalog"))
        except selection.InvalidInput:
            selection.full(self.decision, "selection_input_unreadable")
        # There is no pre-dispatch controller consensus API in stock xdist.
        # Uniformly choose full in every worker instead of independently filtering
        # and hoping post-dispatch collection comparison repairs a mismatch.
        distributed = bool(workerinput) or bool(getattr(config.option, "numprocesses", 0))
        self.is_controller = distributed and not bool(workerinput)
        if distributed:
            selection.full(self.decision, "xdist_full_until_consensus")
        self.attempt_stream = (self.output / ("attempts-" + self.shard + ".jsonl")).open("ab", buffering=0)
        self.capture = None
        if os.environ.get("LEAF_READSET_DIR"):
            self.capture = trace_reads.Capture(self.root).install()
            self.capture.snapshot_modules()

    def test_id(self, nodeid, path=None):
        if nodeid not in self.nodeids:
            try:
                self.nodeids[nodeid] = trace_reads.repo_nodeid(
                    self.root, nodeid, base=self.config.rootpath, path=path)
            except (TypeError, ValueError, OSError, UnicodeError):
                selection.full(self.decision, "module_identity_drift")
                if self.capture is not None:
                    self.capture.mark_incomplete("module_identity_drift")
                self.nodeids[nodeid] = nodeid
        return self.nodeids[nodeid]

    @hook(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        # The wrapper enters before existing quarantine hooks and leaves after them.
        self.collection = [self.test_id(item.nodeid, item.path) for item in items]
        yield
        with self.capture.suspended() if self.capture is not None else nullcontext():
            # Some exclusion hooks only change items. Account for their actual
            # removals before our filter, not merely requested --deselect values.
            remaining = {self.test_id(item.nodeid, item.path) for item in items}
            self.quarantine.update(set(self.collection) - remaining)
            self.decision = validate_collection_or_choose_full(self.collection, self.decision, self.catalog)
            if self.collection_errors:
                selection.full(self.decision, "collection_failed")
            if self.decision.get("apply_filter"):
                modules = set(self.decision["selected_modules"])
                keep, omit = [], []
                for item in items:
                    nodeid = self.test_id(item.nodeid, item.path)
                    (keep if nodeid.split("::", 1)[0] in modules else omit).append(item)
                if not keep:
                    selection.full(self.decision, "empty_selection")
                else:
                    items[:] = keep
                    self.selection_omitted = [self.test_id(item.nodeid, item.path) for item in omit]
                    self.filtering = True
                    try:
                        config.hook.pytest_deselected(items=omit)
                    finally:
                        self.filtering = False
            self.executable_ids = sorted(self.test_id(item.nodeid, item.path) for item in items)
            manifest = {"schema": "leaf.ci.collection.v1", "worker": self.worker,
                        "pid": os.getpid(), "test_ids": sorted(self.collection),
                        "collection_ids_sha256": selection.collection_digest(self.collection),
                        "quarantine_deselected_ids": sorted(self.quarantine),
                        "selection_deselected_ids": self.selection_omitted,
                        "execution_mode": self.decision.get("execution_mode"),
                        "reasons": self.decision.get("reasons", [])}
            selection.write_json(self.output / ("collection-" + self.shard + ".json"), manifest)
        if self.capture is not None:
            self.capture.snapshot_modules()

    def pytest_deselected(self, items):
        if not self.filtering:
            self.quarantine.update(self.test_id(item.nodeid, item.path) for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)
            if self.capture is not None:
                self.capture.mark_incomplete("collection_failed")

    @hook(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        nodeid = self.test_id(item.nodeid, item.path)
        module = nodeid.split("::", 1)[0]
        if self.capture is not None:
            self.capture.begin_module(module, nodeid)
        try:
            yield
        finally:
            if self.capture is not None:
                self.capture.end_module()

    @hook(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if self.capture is not None and fixturedef.scope not in ("function", "class", "module"):
            with self.capture.shared_scope():
                yield
        else:
            yield

    @hook(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        # A finalizer can outlive its apparent test/module scope. Share all reads.
        with self.capture.shared_scope() if self.capture is not None else nullcontext():
            yield

    def pytest_runtest_logreport(self, report):
        with self.capture.suspended() if self.capture is not None else nullcontext():
            nodeid = self.test_id(report.nodeid)
            if report.when == "setup" or nodeid not in self.attempt_numbers:
                self.attempt_numbers[nodeid] = self.attempt_numbers.get(nodeid, 0) + 1
            row = {"schema": "leaf.ci.test-attempt.v1", "nodeid": nodeid,
                   "suite_id": nodeid.split("::", 1)[0],
                   "attempt": self.attempt_numbers[nodeid], "phase": report.when,
                   "outcome": report.outcome, "worker": self.worker, "pid": os.getpid()}
            self.attempts.append(row)
            self.attempt_stream.write(selection.canonical(row) + b"\n")

    @hook(optionalhook=True)
    def pytest_testnodeready(self, node):
        self.worker_started.add(node.gateway.id)

    @hook(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.worker_collections[node.gateway.id] = list(ids)
        values = list(self.worker_collections.values())
        if any(value != values[0] for value in values):
            self.worker_errors.append("worker_collection_disagreement")

    @hook(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        output = getattr(node, "workeroutput", {}).get("leaf_selection_completion")
        if error or not output or output.get("completion_marker") is not True:
            self.worker_errors.append("worker_missing_completion")
        else:
            self.worker_completions[node.gateway.id] = output

    def pytest_sessionfinish(self, session, exitstatus):
        with self.capture.suspended() if self.capture is not None else nullcontext():
            if self.capture is not None:
                self.capture.snapshot_modules()
            self.attempt_stream.close()
            # Web report-only catalogs have no module entries. Keep the real
            # attempt stream beside the startup shards for archive publication.
            web_suite = os.environ.get("LEAF_READSET_SUITE")
            if self.capture is not None and web_suite:
                archive_dir = (Path(os.environ["LEAF_READSET_DIR"]) /
                               trace_reads.encoded_suite(web_suite) /
                               str(int(os.environ.get("LEAF_READSET_ATTEMPT", "1"))))
                archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.output / ("attempts-" + self.shard + ".jsonl"),
                                archive_dir / ("attempts-" + self.shard + ".jsonl"))
            if self.is_controller:
                expected = sorted(tid for entry in self.catalog.get("suites", [])
                                  for tid in entry.get("test_ids", []))
                if (not self.worker_started or
                        self.worker_started != set(self.worker_completions) or
                        self.worker_started != set(self.worker_collections)):
                    self.worker_errors.append("worker_missing_completion")
                if any(sorted(ids) != expected for ids in self.worker_collections.values()):
                    self.worker_errors.append("worker_collection_disagreement")
                self.attempts = [row for worker in sorted(self.worker_completions)
                                 for row in self.worker_completions[worker].get("attempts", [])]
            complete = (int(exitstatus) in (0, 1) and not self.collection_errors and
                        not self.worker_errors)
            if self.is_controller:
                complete = complete and all(row.get("full_run_complete") is True
                                            for row in self.worker_completions.values())
            if not complete and self.capture is not None:
                self.capture.mark_incomplete("session_incomplete")
            entries = self.catalog.get("suites", []) if self.capture is not None else []
            for entry in entries:
                module = entry.get("module")
                if not module:
                    continue
                module = trace_reads.repo_relative_path(self.root, module)
                if module not in self.capture.test_ids:
                    continue
                sid = entry["id"]
                attempt = max((row["attempt"] for row in self.attempts
                               if row["suite_id"] == module), default=1)
                if attempt != 1:
                    self.capture.mark_incomplete("retry_readsets_combined", module)
                destination = (self.output / "readsets" / trace_reads.encoded_suite(sid) /
                               str(attempt) / (self.shard + ".json"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                outcomes_name = "attempts-" + self.shard + ".jsonl"
                shutil.copyfile(self.output / outcomes_name, destination.parent / outcomes_name)
                outcomes_ref = (Path("readsets") / trace_reads.encoded_suite(sid) /
                                str(attempt) / outcomes_name).as_posix()
                readset = trace_reads.write_readset(
                    destination, capture=self.capture, suite_id=sid, module=module,
                    complete=complete, run_id=self.decision.get("build_id") or os.environ.get("LEAF_READSET_RUN"),
                    source_sha=self.decision.get("head_sha") or os.environ.get("LEAF_READSET_SOURCE_SHA"),
                    source_tree=self.decision.get("head_tree") or os.environ.get("LEAF_READSET_SOURCE_TREE"),
                    capture_sha=self.catalog.get("capture_sha") or os.environ.get("LEAF_READSET_CAPTURE_SHA"),
                    catalog_sha256=self.decision.get("catalog_sha256") or
                    self.catalog.get("catalog_sha256") or os.environ.get("LEAF_READSET_CATALOG_SHA256"),
                    attempt=attempt, worker=self.worker, python_only=entry.get("python_only") is True,
                    outcomes_ref=outcomes_ref)
                # Runtime observations cannot re-enable a map. The collector
                # authenticates any miss request before changing trusted state.
                known = self.decision.get("known_read_paths", {}).get(sid)
                if isinstance(known, list):
                    misses = trace_reads.new_read_edges(readset, known, self.decision.get("map_sha"))
                    selection.write_json(destination.with_suffix(".misses.json"), {"misses": misses})
                    for miss in misses:
                        print("SELECTION_MISS " + selection.canonical(miss).decode("ascii"), file=sys.stderr)
            self.decision.update(execution_complete=complete, test_exit_code=int(exitstatus),
                                 tracing_active=self.capture is not None)
            phases = {}
            for row in self.attempts:
                phases.setdefault((row["nodeid"], row["attempt"]), set()).add(row["phase"])
            reported_ids = {row["nodeid"] for row in self.attempts}
            reporting_complete = (bool(phases) and all({"setup", "teardown"} <= value
                                                       for value in phases.values()))
            if not hasattr(self.config, "workerinput") and not self.is_controller:
                reporting_complete = reporting_complete and reported_ids == set(self.executable_ids)
            if self.is_controller:
                reporting_complete = reporting_complete and all(
                    row.get("test_id_reporting_complete") is True for row in self.worker_completions.values())
                quarantined = {tid for row in self.worker_completions.values()
                               for tid in row.get("quarantine_deselected_ids", [])}
                reporting_complete = reporting_complete and reported_ids == set(expected) - quarantined
            completion = {"schema": "leaf.ci.selection-completion.v1",
                          "worker": self.worker, "pid": os.getpid(),
                          "completion_marker": True, "full_run_complete": complete and reporting_complete and
                          self.decision.get("execution_mode") == "full",
                          "test_id_reporting_complete": reporting_complete,
                          "quarantine_deselected_ids": sorted(self.quarantine),
                          "test_exit_code": int(exitstatus), "collection_errors": self.collection_errors,
                          "worker_errors": self.worker_errors,
                          "decision": self.decision, "attempts": self.attempts,
                          "worker_completions": self.worker_completions}
            if hasattr(self.config, "workeroutput"):
                self.config.workeroutput["leaf_selection_completion"] = completion
            selection.write_json(self.output / ("completion-" + self.shard + ".json"), completion)
            detail = dict(self.decision, schema="leaf.ci.selection.v1")
            print("LEAF_SELECTION_FINAL " + selection.canonical(detail).decode("ascii"),
                  file=sys.stderr)
            if self.capture is not None:
                self.capture.close()

    def pytest_unconfigure(self, config):
        if self.capture is not None:
            self.capture.close()
        if not self.attempt_stream.closed:
            self.attempt_stream.close()
