"""Verification-only private PG adapter for immutable E364b platform tests."""
import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import uuid

import pytest

ALIASES = ("DATABASE_URL", "OVERLAY_PG_URL", "ANNOTATION_PG_URL")
AUTHORITY = None
RUN_ROOT = None
SECRETS = []
REPORT = {"source": "364b5f4e21ee72a8a50b9e3c90b572b0faa56cd3", "started": False, "cleanup": None}


def scrub(value):
    text = str(value)
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "[private-test-database]")
    return text


def cleanup():
    global AUTHORITY
    if AUTHORITY is None:
        return REPORT["cleanup"]
    authority, AUTHORITY = AUTHORITY, None
    stopped = authority.stop()
    REPORT["cleanup"] = bool(stopped)
    if stopped and RUN_ROOT is not None:
        try:
            RUN_ROOT.rmdir()
        except OSError:
            REPORT["cleanup"] = False
    REPORT["directory_absent"] = RUN_ROOT is not None and not RUN_ROOT.exists()
    return REPORT["cleanup"] and REPORT["directory_absent"]


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    global AUTHORITY, RUN_ROOT, SECRETS
    platform_cwd = Path(__file__).resolve().parent
    # CI runs from the repository parent, where platform/store.py cannot be
    # imported as top-level store. Remove only that extra package-cwd lookup.
    sys.path[:] = [entry for entry in sys.path
                   if Path(entry or os.getcwd()).resolve() != platform_cwd]
    if os.environ.get("PYTEST_XDIST_WORKER"):
        dsn = os.environ.get("DATABASE_URL", "")
        if not os.environ.get("LEAF_E364B_PG_PARENT") or not dsn:
            raise pytest.UsageError("private PostgreSQL worker authority unavailable")
        SECRETS = [dsn, unquote(urlsplit(dsn).password or "")]
        return
    if any(os.environ.get(name) for name in ALIASES):
        raise pytest.UsageError("ambient database configuration refused")
    root = Path(__file__).resolve().parent.parent
    producer = root / "scripts/proof_project_pg.py"
    REPORT.update(plugin_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  producer_sha256=hashlib.sha256(producer.read_bytes()).hexdigest())
    spec = importlib.util.spec_from_file_location("_e364b_native_pg", producer)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    RUN_ROOT = Path(tempfile.mkdtemp(prefix="leaf-e364b-", dir="/tmp")).resolve()
    os.chmod(RUN_ROOT, 0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    run_id = "e364b-platform-" + uuid.uuid4().hex[:16]
    try:
        AUTHORITY = module.NativePgAuthority.start(run_id, run_root=RUN_ROOT, port=port,
            environ=dict(os.environ, LEAF_PG_BIN="/opt/homebrew/opt/postgresql@16/bin"))
        dsn = AUTHORITY.dsn()
        SECRETS = [dsn, unquote(urlsplit(dsn).password or "")]
        for name in ALIASES:
            os.environ[name] = dsn
        os.environ["LEAF_E364B_PG_PARENT"] = str(os.getpid())
        os.environ["LEAF_GATE_REQUIRE_DATABASE"] = "1"
        REPORT.update(started=True, run_id=run_id, postgres_major=16)
        atexit.register(cleanup)
    except BaseException:
        cleanup()
        if RUN_ROOT.exists() and not any(RUN_ROOT.iterdir()):
            RUN_ROOT.rmdir()
        raise pytest.UsageError("private PostgreSQL initialization failed") from None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.longrepr is not None:
        report.longrepr = scrub(report.longrepr)
    report.sections = [(name, scrub(text)) for name, text in report.sections]


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report):
    if report.longrepr is not None:
        report.longrepr = scrub(report.longrepr)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        root = Path(__file__).resolve().parent.parent
        store = sys.modules.get("store")
        origin = Path(getattr(store, "__file__", "")).resolve() if store else None
        session.config.workeroutput["e364b_import_proof"] = {
            "store_origin": origin.relative_to(root).as_posix() if origin is not None and root in origin.parents else "unexpected",
            "store_source_sha256": hashlib.sha256(origin.read_bytes()).hexdigest() if origin is not None and root in origin.parents and origin.is_file() else None,
            "platform_cwd_on_sys_path": any(Path(p or os.getcwd()).resolve() == root / "platform" for p in sys.path),
        }
        return
    if REPORT["started"]:
        if not cleanup():
            session.exitstatus = 2
        print("E364B_PRIVATE_PG " + json.dumps(REPORT, sort_keys=True), flush=True)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    REPORT["worker_import_proof"] = node.workeroutput.get("e364b_import_proof", {})


def pytest_unconfigure(config):
    cleanup()
