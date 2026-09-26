"""Trusted process-trace capture wrapper. Capture failure never decides CI."""

import argparse
import copy
import hashlib
import json
import math
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from . import external_inventory as inventory
    from . import trace_process_tree as tree
except ImportError:
    import external_inventory as inventory
    import trace_process_tree as tree


TRACE_FLAGS = ["-f", "-ttt", "-T", "-v", "-xx", "-yy", "-s", "65536", "-e", "trace=all"]
ADMISSION_POLICY = {
    "python-installation": True, "os-image": True, "terraform-provider-cache": True,
    "generated": False, "generated-input": False, "device": False, "network": False,
    "filesystem-capacity": False, "unresolved_external": False, "supervisor": False,
}


def file_digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def facility_profile():
    if os.name == "nt":
        return {"ptrace_scope": None, "Seccomp": None, "CapEff": None,
                "CapBnd": None, "NoNewPrivs": None}
    profile = {"ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip()}
    rows = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    for name in ("Seccomp", "CapEff", "CapBnd", "NoNewPrivs"):
        profile[name] = rows[name].strip()
    return profile


def check_facility(tracer):
    info = {"available": False, "reason": None, "version": None, "exe_sha256": None,
            "facility_profile": None, "kernel_release": None, "machine": None}
    executable = shutil.which(tracer[0])
    if executable is None:
        info["reason"] = "tracer_missing"
        return info
    try:
        info["exe_sha256"] = file_digest(executable)
        version = subprocess.run(tracer + ["--version"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=10, check=False)
        line = version.stdout.splitlines()[0].decode("ascii", "strict") if version.stdout else ""
        if version.returncode or not line.startswith("strace -- version 5.16"):
            info["reason"] = "tracer_version_unsupported"
            return info
        # Retain only a recognized version, never arbitrary diagnostic text.
        info["version"] = line
        info["facility_profile"] = facility_profile()
        if hasattr(os, "uname"):
            uname = os.uname()
            info.update(kernel_release=uname.release, machine=uname.machine)
        info["available"] = True
    except (OSError, ValueError, KeyError, UnicodeError, subprocess.SubprocessError):
        info["reason"] = "facility_probe_failed"
    return info


def build_epoch_manifest(context, facility, tracer_argv, inventory_limits=None):
    """Side-effect free: reads helper blobs and admissible external roots only."""
    helpers = context.get("helper_blobs", {})
    return {
        "schema": "leaf.ci.capture-epoch.v1",
        "trace_process_tree_sha256": file_digest(tree.__file__),
        "trace_supervisor_sha256": file_digest(__file__),
        "trace_reads_sha256": helpers.get("trace_reads_sha256"),
        "reporting_helper_sha256s": helpers.get("reporting_helper_sha256s", {}),
        "strace_version": facility["version"],
        "strace_package": context.get("strace_package"),
        "strace_exe_sha256": facility["exe_sha256"],
        "tracer_argv": tracer_argv,
        "syscall_policy_digest": tree.digest(tree.POLICY),
        "kernel_release": facility["kernel_release"], "machine": facility["machine"],
        "facility_profile": facility["facility_profile"], "parser_version": tree.PARSER_VERSION,
        "admission_policy_digest": tree.digest(ADMISSION_POLICY),
        "toolchain_fingerprint": context.get("toolchain_fingerprint"),
        "external_root_table": context["external_roots"],
        # A changed installed byte under an admissible root changes the epoch.
        "external_root_digests": inventory.digest_roots(context["external_roots"], inventory_limits),
        "image_manifest_digest": context.get("image_manifest_digest"),
        "distribution_list_digest": context.get("distribution_list_digest"),
        "interpreter_identity": context.get("interpreter_identity"),
    }


def capture_epoch(manifest):
    return tree.digest(manifest)


def external_identities(result):
    table = {}
    for token, observed in result["external_paths"].items():
        row = {"identity_sha256": None, "size": None, "class": observed["class"],
               "inventory_ref": None, "resolved": False, "reason": "class_not_admissible"}
        table[token] = row
        admissible = ADMISSION_POLICY.get(observed["class"], False)
        try:
            # Reject devices and directories before reading them. O_NONBLOCK
            # also prevents a raced FIFO from hanging identity collection.
            before = os.stat(observed["path"])
            if not stat.S_ISREG(before.st_mode):
                raise OSError("not_regular")
            fd = os.open(observed["path"], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("not_regular")
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    hasher.update(chunk)
                after = os.fstat(stream.fileno())
            row.update(identity_sha256=hasher.hexdigest(), size=after.st_size)
            expected = observed.get("size")
            if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or (expected is not None and expected != after.st_size)):
                row["reason"] = "size_changed"
            else:
                row.update(resolved=admissible, reason=("size_unverified" if expected is None else None)
                           if admissible else "class_not_admissible")
        except (OSError, ValueError):
            row["reason"] = "identity_unreadable"
        if observed.get("written"):
            row.update(resolved=False, reason="written_during_run")
    return table


def _command(command):
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode
    except OSError:
        return 127


def _command_child(argv):
    # The tracer's status need not be the test's status. A small wrapper waits
    # for the command and records only its numeric outcome in the private dir.
    status_path, command = argv[0], argv[1:]
    code = _command(command)
    tree._atomic(Path(status_path), {"command_exit_code": code})
    return code


def _stream_file(path, process, grace, errors):
    process.wait()
    try:
        with open(path, "rb") as stream:
            # A regular file has no writer-close notification. Observe one
            # grace interval after tracer exit to catch inherited writers.
            deadline = time.monotonic() + grace
            initial_size = os.fstat(stream.fileno()).st_size
            while True:
                chunk = stream.read(65536)
                if chunk:
                    yield chunk
                    if stream.tell() > initial_size and time.monotonic() >= deadline:
                        errors.append("descendant_outlived_tree")
                        return
                elif time.monotonic() >= deadline:
                    return
                else:
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    except OSError:
        errors.append("trace_sink_unreadable")


def _stream_fifo(fd, process, grace, errors):
    deadline = None
    while True:
        if process.poll() is not None and deadline is None:
            deadline = time.monotonic() + grace
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                chunk = None
            if chunk:
                yield chunk
            elif chunk == b"" and process.poll() is not None:
                return
            elif chunk == b"":
                time.sleep(0.01)
        if deadline is not None and time.monotonic() >= deadline:
            errors.append("descendant_outlived_tree")
            return


def _incomplete(result, errors):
    if not errors:
        return
    certificate = result["certificate"]
    reasons = sorted(set(certificate["reasons"] + errors))
    certificate.update(complete=False, reasons=reasons, reason_count=len(reasons))
    for shard in result["shards"]:
        shard.update(capture_complete=False, incomplete_reasons=reasons,
                     process_tree_sha256=tree.digest(certificate))


def load_suites(path):
    """Read the plugin's session-finish suite list; any defect is suites_unavailable."""
    try:
        suites = json.loads(Path(path).read_bytes().decode("utf-8", "strict"), object_pairs_hook=tree._pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
        suites = [{key: row[key] for key in ("suite_id", "attempt", "worker", "test_ids", "outcomes_ref")}
                  for row in tree.validate_suites(suites)] if isinstance(suites, list) else None
        if not suites:
            raise ValueError("suites_unavailable")
        tree.canonical(suites)
        return suites
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def run(context, out_dir, command, tracer=None, sink=None, limits=None, descendant_grace=30, suites_file=None):
    context = copy.deepcopy(context)
    # The tracee pid exists only once the tracer runs: the decoder adopts the
    # stream's first pid as root, never a producer-supplied guess.
    if isinstance(context, dict):
        context.pop("root_pid", None)
        if suites_file is not None:
            context["suites_deferred"] = True
    tree._validate(context)
    for name, value in (limits or {}).items():
        if name not in tree.LIMITS or type(value) is not int or value < 1:
            raise ValueError("invalid_limit")
    if not command or not math.isfinite(descendant_grace) or descendant_grace < 0:
        raise ValueError("invalid_command_or_grace")
    sink = sink or ("fifo" if os.name == "posix" else "file")
    if sink not in {"fifo", "file"}:
        raise ValueError("invalid_sink")
    if sink == "fifo" and os.name == "nt":
        raise ValueError("fifo sink is unavailable on Windows; use --sink file")
    tracer = list(tracer or ["strace"])
    if not tracer:
        raise ValueError("invalid_tracer")
    context["seed_fds"]["0"] = {"kind": "devnull"}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    facility = check_facility(tracer)
    # The command argv is never published. The capture argv is exact through
    # -o; its private, per-run sink is represented by a stable slot in the epoch.
    trace_argv = tracer + TRACE_FLAGS + ["-o", "<private-" + sink + ">", "--"]
    receipt = {key: context[key] for key in tree.BINDINGS}
    receipt.update(schema="leaf.ci.trace-receipt.v1", capture_group=context["capture_group"],
                   facility_available=facility["available"], facility_profile=facility["facility_profile"],
                   facility_reason=facility["reason"], kernel_release=facility["kernel_release"], machine=facility["machine"],
                   tracer={"argv": trace_argv, "version": facility["version"], "exe_sha256": facility["exe_sha256"]},
                   command_argv_sha256=tree.digest(command), command_exit_code=None, tracer_exit_code=None, root_pid=None,
                   byte_count=0, sha256=hashlib.sha256(b"").hexdigest(), elapsed_seconds=None,
                   baseline_elapsed_seconds=context.get("baseline_elapsed_seconds"),
                   decoder_complete=False, capture_complete=False, capture_errors=[],
                   sanitized_trace_bytes=0, decoded_evidence_bytes=0, decoder_peak_bytes=0)
    receipt_path = out / "reports" / ("trace-receipt-" + context["capture_group"] + ".json")
    if not facility["available"]:
        receipt["capture_errors"] = [facility["reason"]]
        receipt["command_exit_code"] = _command(command)
    else:
        manifest = build_epoch_manifest(context, facility, trace_argv)
        context.update(capture_epoch=capture_epoch(manifest), capture_epoch_manifest=manifest)
        errors = receipt["capture_errors"]
        with tempfile.TemporaryDirectory(prefix=".trace-", dir=out) as private:
            path = os.path.join(private, "sink")
            status_path = os.path.join(private, "command-exit.json")
            fd = None
            if sink == "fifo":
                os.mkfifo(path, 0o600)
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            wrapped = [sys.executable, os.path.abspath(__file__), "_command", status_path] + command
            actual_argv = tracer + TRACE_FLAGS + ["-o", path, "--"] + wrapped
            receipt["tracer"]["argv"] = tracer + TRACE_FLAGS + ["-o", path, "--"]
            try:
                try:
                    process = subprocess.Popen(actual_argv, stdin=subprocess.DEVNULL)
                except OSError:
                    receipt.update(facility_available=False, facility_reason="tracer_launch_failed")
                    errors.append("tracer_launch_failed")
                    receipt["command_exit_code"] = _command(command)
                    process = None
                if process is not None:
                    hasher, count = hashlib.sha256(), 0

                    def chunks():
                        nonlocal count
                        stream = (_stream_fifo(fd, process, descendant_grace, errors) if sink == "fifo"
                                  else _stream_file(path, process, descendant_grace, errors))
                        for chunk in stream:
                            hasher.update(chunk)
                            count += len(chunk)
                            yield chunk
                        context["terminal_receipt"] = {"byte_count": count, "sha256": hasher.hexdigest()}
                        # Deferred suites are read at stream end, before outputs are built.
                        if context.get("suites_deferred") is True:
                            suites = load_suites(suites_file) if suites_file is not None else None
                            if suites is None:
                                errors.append("suites_unavailable")
                                context["suites"] = []
                            else:
                                context["suites"] = suites

                    result = tree.decode_stream(chunks(), context, limits)
                    receipt["tracer_exit_code"] = process.wait()
                    try:
                        code = json.loads(Path(status_path).read_text())["command_exit_code"]
                        if type(code) is not int:
                            raise ValueError("invalid_command_status")
                        receipt["command_exit_code"] = code
                    except (OSError, ValueError, KeyError):
                        errors.append("command_outcome_unavailable")
                        receipt["command_exit_code"] = receipt["tracer_exit_code"] or 127
                    if receipt["tracer_exit_code"] != 0:
                        errors.append("tracer_nonzero_exit")
                    tree.bind_external_identities(result, external_identities(result))
                    _incomplete(result, errors)
                    tree.write_outputs(result, out)
                    certificate = result["certificate"]
                    receipt.update(byte_count=count, sha256=hasher.hexdigest(), sanitized_trace_bytes=count,
                                   root_pid=certificate["root"]["pid"],
                                   decoder_complete=certificate["decoder_complete"], capture_complete=certificate["complete"],
                                   capture_errors=sorted(set(errors + certificate["reasons"])),
                                   decoder_peak_bytes=result["decoder_peak_bytes"],
                                   decoded_evidence_bytes=sum(len(tree.canonical(value)) + 1 for value in [certificate] + result["shards"]))
            finally:
                if fd is not None:
                    os.close(fd)
    receipt["elapsed_seconds"] = time.monotonic() - started
    tree._atomic(receipt_path, receipt)
    return receipt["command_exit_code"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_command":
        return _command_child(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    launch = sub.add_parser("run")
    launch.add_argument("--context", required=True)
    launch.add_argument("--out", required=True)
    launch.add_argument("--tracer", nargs="+", default=["strace"])
    launch.add_argument("--sink", choices=("fifo", "file"))
    launch.add_argument("--limit", action="append", default=[])
    launch.add_argument("--descendant-grace", type=float, default=30)
    launch.add_argument("--suites-file", help="JSON suite list the plugin writes at session finish")
    # Split explicitly so a multi-token tracer cannot swallow COMMAND.
    boundary = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    try:
        context = json.loads(Path(args.context).read_text(encoding="utf-8"), object_pairs_hook=tree._pairs)
        limits = {}
        for item in args.limit:
            name, value = item.split("=", 1)
            limits[name] = int(value)
        return run(context, args.out, command, args.tracer, args.sink, limits, args.descendant_grace,
                   args.suites_file)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        if args.sink == "fifo" and os.name == "nt":
            print("trace_supervisor: fifo sink is unavailable on Windows; use --sink file", file=sys.stderr)
        else:
            print("trace_supervisor: invalid context, limit, command or output", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
