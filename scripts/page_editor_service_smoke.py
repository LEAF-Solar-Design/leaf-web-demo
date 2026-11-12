#!/usr/bin/env python3
"""Private Linux page-service producer. No providers, mock page routes or tokens."""
import argparse
import contextlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import time
import traceback
import uuid


def prerequisites():
    # These refusals must work even without any optional server dependencies.
    if sys.platform != "linux":
        print("PAGE_SMOKE LINUX_REQUIRED", file=sys.stderr)
        return 2
    if not os.environ.get("DATABASE_URL", "").strip():
        print("PAGE_SMOKE DATABASE_REQUIRED", file=sys.stderr)
        return 3
    return 0


def load_package(root):
    # Match the gate migration loader; never shadow stdlib platform.
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != root]
    sys.path.insert(0, str(root / "server"))
    pkg = root / "platform"
    spec = importlib.util.spec_from_file_location(
        "leaf_platform", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["leaf_platform"] = module
    spec.loader.exec_module(module)


def scrubber(root):
    spec = importlib.util.spec_from_file_location("page_smoke_gate", root / "scripts/run-all-gates.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.scrub_evidence


class RedactedLog:
    def __init__(self, stream, scrub):
        self.stream, self.scrub = stream, scrub
        self.pending = ""

    def emit(self, text):
        # Also remove the exact DSN, including passwordless connection strings.
        dsn = os.environ.get("DATABASE_URL", "")
        self.stream.write(self.scrub(text.replace(dsn, "[database]") if dsn else text))
        self.stream.flush()

    def write(self, text):
        # print() can split a credential label and value across writes.
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.emit(line + "\n")
        return len(text)

    def flush(self):
        if self.pending:
            self.emit(self.pending)
            self.pending = ""
        self.stream.flush()


def serve(args, started):
    root = Path(__file__).resolve().parents[1]
    os.environ["LEAF_AUTH_LIVE"] = "0"
    load_package(root)
    from leaf_platform import db, project_lifecycle, store
    from leaf_platform.api import router
    from fastapi import FastAPI
    import uvicorn

    print("PAGE_SMOKE MIGRATION_START")
    db.apply_migration()
    print(f"PAGE_SMOKE MIGRATION_DONE elapsed_s={time.monotonic() - started:.3f}")
    run_id = uuid.uuid4().hex
    name = f"Page service smoke {run_id[:8]}"
    org = store.create_org(name=name, org_id=uuid.uuid4())
    actor = store.create_identity_binding(org.org_id, "auth0", str(uuid.uuid4()), role="owner")
    created = project_lifecycle.create_blank_project(
        org.org_id, actor.binding_id, name=name, idempotency_key=str(uuid.uuid4()))
    print(f"PAGE_SMOKE SEED_DONE elapsed_s={time.monotonic() - started:.3f}")
    ready = {"schema": "leaf.page-smoke.v1", "api_origin": f"http://127.0.0.1:{args.port}",
             "org_id": str(org.org_id), "binding_id": str(actor.binding_id),
             "project_id": created["project"]["project_id"], "project_name": name}
    app = FastAPI()
    app.include_router(router)

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                temporary = args.ready_file.with_name(f".ready-{run_id}.tmp")
                temporary.write_text(json.dumps(ready), encoding="utf-8")
                temporary.replace(args.ready_file)
                print(f"PAGE_SMOKE READY startup_s={time.monotonic() - started:.3f}")

    try:
        # Uvicorn owns its signal handlers and drains on SIGTERM; no child processes.
        ReadyServer(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                  log_config=None, access_log=True,
                                  timeout_graceful_shutdown=10)).run()
    finally:
        db.reset_pool()
        args.ready_file.unlink(missing_ok=True)


def main(argv=None):
    refusal = prerequisites()
    if refusal:
        return refusal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.unlink(missing_ok=True)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    scrub = scrubber(Path(__file__).resolve().parents[1])
    with args.log_file.open("w", encoding="utf-8") as stream:
        log = RedactedLog(stream, scrub)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            logging.basicConfig(stream=log, level=logging.INFO, force=True)
            try:
                serve(args, started)
            except BaseException:
                print(f"PAGE_SMOKE START_OR_RUN_FAILED elapsed_s={time.monotonic() - started:.3f}")
                traceback.print_exc()
                return 1
            finally:
                log.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
