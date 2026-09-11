"""Offline startup proof for the separately built canonical ARLO consumer.

Run inside the image with --network none, no credentials, and this file mounted
read-only. This verifies installed imports and dispatch up to the database
boundary. It does not prove database readiness, job execution, or learned CUDA
inference. Container execution remains an AWS-only build-owner operation.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch


def main() -> None:
    if any(os.environ.get(name) for name in
           ("DATABASE_URL", "ARLO_MODEL_BUNDLE", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")):
        raise RuntimeError("Offline smoke requires an environment without credentials or a model bundle")

    # Fail even if a future import attempts a network connection before the
    # schema/serve boundary. The outer container must also use --network none.
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen",
                     "os.system", "os.exec", "os.posix_spawn"}:
            raise RuntimeError(f"Offline startup attempted prohibited operation: {event}")

    sys.addaudithook(audit)
    root = Path("/opt/leaf")
    sys.path.insert(0, str(root / "server"))
    for name in ("arlo.design.__main__", "canonical_worker",
                 "solver_adapters.arlo_design", "solver_adapters.autofill"):
        importlib.import_module(name)
    worker = importlib.import_module("canonical_worker")
    # Import the actual packaged database dependencies; replace only the first
    # live schema operation and the polling loop, never the imported modules.
    database = worker.platform_link._load_platform()[1]
    entrypoint = root / "arlo_consumer_entrypoint.py"
    with patch.object(sys, "argv", [str(entrypoint)]), patch("os.execv") as execute:
        runpy.run_path(str(entrypoint), run_name="__main__")
    expected = [sys.executable, str(root / "server/canonical_worker.py"),
                "--tool", "arlo-design", "--poll-seconds", "1"]
    execute.assert_called_once_with(sys.executable, expected)
    if os.environ["ARLO_SOLVER_ROOT"] != "/opt/arlo":
        raise AssertionError("Entrypoint selected an unexpected solver root")
    with patch.object(sys, "argv", expected[1:]), \
         patch.object(database, "assert_schema_current") as schema, \
         patch.object(worker, "serve") as serve:
        worker.main()
    schema.assert_called_once_with()
    serve.assert_called_once()
    if serve.call_args.kwargs["tool_name"] != "arlo-design":
        raise AssertionError("Canonical worker selected a different tool")
    paths = [entrypoint, root / "server/canonical_worker.py",
             root / "server/solver_adapters/arlo_design.py",
             Path("/opt/arlo/arlo/design/candidates.py")]
    print(json.dumps({
        "schema": "leaf.arlo-consumer-offline-smoke.v1",
        "imports": "passed", "entrypoint_dispatch": "arlo-design",
        "database_schema_verified": False, "job_claimed": False,
        "learned_inference_verified": False,
        "files_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in paths},
    }, sort_keys=True))


if __name__ == "__main__":
    main()
