"""A fresh process must not expose a partially imported electrical kernel."""
import os
from pathlib import Path
import subprocess
import sys


def test_drawing_readiness_during_cold_cabling_import(tmp_path):
    result = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).resolve()), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_sibling_loaders_share_lock_from_any_cwd(tmp_path):
    result = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).resolve()), str(tmp_path), "siblings"],
        cwd=tmp_path, capture_output=True, text=True,
        timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stdout + result.stderr


def _sibling_process():
    import concurrent.futures
    import importlib.machinery
    import importlib.util
    import threading

    server = Path(__file__).resolve().parents[1]
    sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != server]
    spec = importlib.util.spec_from_file_location(
        "solar_inverter_cabling", server / "solar_inverter_cabling.py")
    cabling = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cabling
    spec.loader.exec_module(cabling)
    cabling._load_sibling("solar_design_graph")
    bridge = cabling._load_sibling("solar_electrical_state_bridge")
    assert bridge.cab is cabling
    assert bridge.st is cabling.st

    name = "_leaf_sibling_probe"
    paused, release, started = threading.Event(), threading.Event(), threading.Event()
    execute = importlib.machinery.SourceFileLoader.exec_module
    executions = []

    def probe(loader, module):
        if module.__name__ != name:
            return execute(loader, module)
        assert Path(loader.path) == server / (name + ".py")
        executions.append(module)
        assert bridge._load_sibling(name) is module  # same-thread circular load
        paused.set()
        assert release.wait(20), "sibling import was never released"
        module.ready = True

    def competing_load():
        started.set()
        return bridge._load_sibling(name)

    importlib.machinery.SourceFileLoader.exec_module = probe
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cabling._load_sibling, name)
            try:
                assert paused.wait(20), "sibling did not start cold"
                second = pool.submit(competing_load)
                assert started.wait(20), "competing loader did not start"
                try:
                    second.result(timeout=0.1)
                except concurrent.futures.TimeoutError:
                    pass
                else:
                    raise AssertionError("competing loader exposed a partial module")
            finally:
                release.set()
            left, right = first.result(timeout=20), second.result(timeout=20)
        assert left is right and left.ready
        assert executions == [left]
        assert left._leaf_sibling_completed is True

        del sys.modules[name]

        def failing(loader, module):
            if module.__name__ == name:
                raise RuntimeError("probe failed")
            return execute(loader, module)

        importlib.machinery.SourceFileLoader.exec_module = failing
        for load in (bridge._load_sibling, cabling._load_sibling):
            try:
                load(name)
            except RuntimeError as exc:
                assert str(exc) == "probe failed"
            else:
                raise AssertionError("sibling failure was swallowed")
            assert name not in sys.modules
        importlib.machinery.SourceFileLoader.exec_module = probe
        assert bridge._load_sibling(name).ready
    finally:
        release.set()
        sys.modules.pop(name, None)
        importlib.machinery.SourceFileLoader.exec_module = execute


def _cold_process(root):
    import concurrent.futures
    import hashlib
    import importlib
    import importlib._bootstrap as bootstrap
    import importlib.machinery
    import json
    import threading

    server = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(server))
    os.environ.update(LEAF_DRAWING_STORE="legacy", LEAF_DRAWING_MUTATIONS_ENABLED="1",
                      LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED="1")
    os.environ.pop("LEAF_DRAWING_MUTATIONS_FENCE_FILE", None)
    import write_loop
    import store
    import solar_local_graph as local
    import product_capability_availability as availability

    tenant, drawing, holder = "fixture-tenant", "cold-solar", "cold-owner"
    backend = store.FilesystemBackend(str(root / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [],
              "faces3d": [], "blockdefs": [], "geodata": None}
    source = root / "seed.json"
    source.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    _, key = store.resolve_version(backend, tenant, drawing, 1)
    params = {"drawing_id": drawing, "expected_rev": 0,
              "changes": {"panels_in_sequence": 3}, "initialize": {
                  "schema_version": 1,
                  "source_intake_sha256": hashlib.sha256(backend.get(key)).hexdigest(),
                  "units": {"drawing_units": "ft", "wcs_to_ucs": [
                      1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                      "elevation_datum": "unknown", "crs": None}}}
    fence = store.acquire_checkout_fence(backend, tenant, drawing, holder, 300)
    try:
        result = local.run_local_graph_commit(
            backend, tenant, "solar-settings", params, drawing_id=drawing,
            source_version=1, holder=holder, fence=fence, job_id="cold-seed")
    finally:
        store.release_checkout(backend, tenant, drawing, holder)
    assert result["new_version"]["version"] == 2
    write_loop.backend_for_tenant = lambda *args, **kwargs: backend
    assert "solar_inverter_cabling" not in sys.modules
    assert "solar_electrical_state_bridge" not in sys.modules

    paused, release, contended = threading.Event(), threading.Event(), threading.Event()
    start = threading.Barrier(3)
    execute = importlib.machinery.SourceFileLoader.exec_module
    acquire = bootstrap._ModuleLock.acquire

    def slow_cabling(loader, module):
        if module.__name__ == "solar_inverter_cabling":
            paused.set()
            assert release.wait(20), "cold import was never released"
        return execute(loader, module)

    def observed_acquire(lock, *args, **kwargs):
        # Standard imports wait here. The old sibling loader skips this lock,
        # reads the half-built module and completes with AttributeError instead.
        if lock.name == "solar_inverter_cabling" and threading.current_thread().name.startswith("readiness"):
            contended.set()
        return acquire(lock, *args, **kwargs)

    def read():
        start.wait(timeout=20)
        try:
            return availability.w1_input_readiness(tenant, drawing, version=2)
        finally:
            contended.set()

    importlib.machinery.SourceFileLoader.exec_module = slow_cabling
    bootstrap._ModuleLock.acquire = observed_acquire
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as importer, \
                concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="readiness") as readers:
            cold = importer.submit(importlib.import_module, "solar_inverter_cabling")
            try:
                assert paused.wait(20), "cabling did not start cold"
                first, second = readers.submit(read), readers.submit(read)
                start.wait(timeout=20)
                assert contended.wait(20), "readiness never reached the cold import"
            finally:
                release.set()
            cabling = cold.result(timeout=20)
            left, right = first.result(timeout=20), second.result(timeout=20)
        assert left == right
        assert left["solar-settings"] == {"input_ready": True, "input_reason": None}
        bridge = importlib.import_module("solar_electrical_state_bridge")
        assert bridge.cab is cabling
        assert bridge.st is cabling.st
    finally:
        release.set()
        importlib.machinery.SourceFileLoader.exec_module = execute
        bootstrap._ModuleLock.acquire = acquire


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "siblings":
        _sibling_process()
    else:
        _cold_process(Path(sys.argv[1]))
