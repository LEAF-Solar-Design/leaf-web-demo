"""Build the capture-epoch manifest for a build that runs no tracer.

A selecting build needs the epoch a tracing build would record, so the selector can
compare it with the map's. This probe reproduces trace_supervisor.run's preamble by
calling the supervisor's own functions, never copies of them, so the two cannot drift.

Exit codes: 0 manifest written; 3 facility unavailable (nothing written); 2 any other
failure (nothing written). Fails closed: a partial manifest is never published.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

try:
    from . import trace_supervisor as supervisor
except ImportError:
    import trace_supervisor as supervisor


EXIT_OK, EXIT_FAILED, EXIT_FACILITY = 0, 2, 3


class FacilityUnavailable(Exception):
    """The tracer facility is missing or unsupported: no epoch can be claimed."""


def probe(context, tracer=None, sink=None, environ=None):
    """The manifest trace_supervisor.run would build for context; raises FacilityUnavailable."""
    if not isinstance(context, dict) or not isinstance(context.get("external_roots"), dict):
        raise ValueError("invalid_context")
    environ = os.environ if environ is None else environ
    context = copy.deepcopy(context)
    sink = sink or ("fifo" if os.name == "posix" else "file")
    if sink not in {"fifo", "file"}:
        raise ValueError("invalid_sink")
    tracer = list(tracer or ["strace"])
    if not tracer:
        raise ValueError("invalid_tracer")
    # run(): reuse the producer's interpreter identity, including a failed path.
    identity = context.get("interpreter_identity")
    interpreter = identity.get("executable") if isinstance(identity, dict) else identity
    if interpreter is None:
        interpreter = environ.get("LEAF_TRACE_INTERPRETER", sys.executable)
        context["interpreter_identity"] = {"executable": interpreter}
    context["external_roots"] = supervisor.inventory.resolve_default_roots(
        environ, interpreter=interpreter, table=context["external_roots"])
    report = supervisor.inventory.resolve_default_roots.last_report.copy()
    context.update(roots_source=report["roots_source"], roots_interpreter=report["roots_interpreter"])
    facility = supervisor.check_facility(tracer)
    if not facility["available"]:
        raise FacilityUnavailable(facility["reason"])
    trace_argv = tracer + supervisor.TRACE_FLAGS + ["-o", "<private-" + sink + ">", "--"]
    return supervisor.build_epoch_manifest(context, facility, trace_argv), facility


def _write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _profile(facility):
    """One token for the LEAF_EVENT line: the facility profile's digest, never raw text."""
    return supervisor.tree.digest(facility.get("facility_profile"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tracer", nargs="+", default=["strace"])
    parser.add_argument("--sink", choices=("fifo", "file"))
    args = parser.parse_args(argv)
    try:
        context = json.loads(Path(args.context).read_bytes().decode("utf-8", "strict"),
                             object_pairs_hook=supervisor.tree._pairs)
        manifest, facility = probe(context, args.tracer, args.sink)
        data = supervisor.tree.canonical(manifest) + b"\n"
        epoch = supervisor.normalized_epoch(manifest)
        _write_atomic(args.out, data)
    except FacilityUnavailable as exc:
        print("epoch_probe: facility_unavailable " + str(exc), file=sys.stderr)
        return EXIT_FACILITY
    except Exception as exc:  # any failure publishes nothing; the class alone is reported
        print("epoch_probe: " + type(exc).__name__, file=sys.stderr)
        return EXIT_FAILED
    print("LEAF_EVENT epoch_probe capture_epoch=" + epoch + " facility=" + _profile(facility), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
