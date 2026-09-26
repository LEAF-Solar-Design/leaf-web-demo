"""Trusted process-trace capture wrapper. Capture failure never decides CI."""

import argparse
import copy
import hashlib
import json
import math
import os
import select
import shutil
import signal
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


# Amendment 5: -x escapes non-ASCII bytes only, -s 4096 covers PATH_MAX, and the noise list
# (policy data, digested into syscall_policy_digest) is filtered in the kernel by
# --seccomp-bpf, so those syscalls cost no ptrace stop. No durations, no fd annotations:
# sequence orders events and the descriptor table supplies provenance.
TRACE_FLAGS = ["-f", "-ttt", "-v", "-x", "-s", "4096", "--seccomp-bpf", "-e", "trace=!" + ",".join(tree.POLICY["noise"])]
TERM, KILL = signal.SIGTERM, getattr(signal, "SIGKILL", 9)
KILL_GRACE = 10  # seconds between SIGTERM and SIGKILL of the tracer's group [guessed]
STDERR_TAIL = 4096  # bytes of strace's own diagnostics copied into the receipt
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


def _command(command, stderr=None):
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stderr=stderr, check=False).returncode
    except OSError:
        return 127


def _stderr_token():
    """Duplicate this process's stderr for the command wrapper: (fd, token), or (None, "-").

    strace's own stderr goes to a private file; the wrapper hands the command this
    duplicate so tracee output reaches the CI log and never the receipt.
    """
    try:
        fd = os.dup(2)
    except OSError:
        return None, "-"
    try:
        if os.name == "nt":
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
            os.set_handle_inheritable(handle, True)
            return fd, "h" + str(handle)
        os.set_inheritable(fd, True)
        info = os.fstat(fd)
        return fd, "%d:%d:%d" % (fd, info.st_dev, info.st_ino)
    except (OSError, ValueError):
        os.close(fd)
        return None, "-"


def _restored_stderr(token):
    """The supervisor's stderr duplicate, or None when it did not survive the tracer (fails closed)."""
    try:
        if token.startswith("h"):
            import msvcrt
            return msvcrt.open_osfhandle(int(token[1:]), 0)
        fd, dev, ino = (int(part) for part in token.split(":"))
        info = os.fstat(fd)
        # A reused descriptor number is not our stderr.
        return fd if (info.st_dev, info.st_ino) == (dev, ino) else None
    except (OSError, ValueError, ImportError):
        return None


def _command_child(argv):
    # The tracer's status need not be the test's status. A small wrapper waits
    # for the command and records only its numeric outcome in the private dir.
    status_path, token, command = argv[0], argv[1], argv[2:]
    stderr = _restored_stderr(token)
    code = _command(command, stderr)
    tree._atomic(Path(status_path), {"command_exit_code": code, "stderr_restored": stderr is not None})
    return code


def _stderr_tail(path, limit=STDERR_TAIL):
    """Last limit bytes of the tracer's stderr as printable ASCII (one char per byte), or None."""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
    except OSError:
        return None
    return "".join(c if c in "\n\r\t" or " " <= c <= "~" else "?" for c in data.decode("latin-1"))


def _signal_group(process, sig):
    """Signal the tracer's process group (POSIX) or the tracer (Windows); False once gone."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == 0:
            return process.poll() is None
        elif sig == KILL:
            process.kill()
        else:
            process.terminate()
        return True
    except OSError:
        # ESRCH: the group is empty. EPERM: only zombies remain (macOS).
        return False


def terminate_tree(process, grace, send=None, escalate=None):
    """SIGTERM the tracer's group, wait up to grace seconds for it to empty, then SIGKILL it.

    Bounded: never waits longer than grace. escalate() turning true (a second signal)
    ends the grace at once. Returns the signals sent, in order.
    """
    send = send or _signal_group
    send(process, TERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not (escalate and escalate()):
        if process.poll() is not None and not send(process, 0):
            break
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    # Survivors of the group (a tracee that ignored SIGTERM) never outlive the grace.
    send(process, KILL)
    return [TERM, KILL]


def _install_forwarding(handler):
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not the main thread: nothing can be forwarded from here.
            pass
    return previous


def _restore_forwarding(previous):
    for sig, handler in previous.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def _stream_file(path, process, grace, errors):
    # Poll, never block in wait(): a forwarded signal's handler must be able to reap.
    while process.poll() is None:
        time.sleep(0.02)
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


def run(context, out_dir, command, tracer=None, sink=None, limits=None, descendant_grace=30, suites_file=None,
        kill_grace=KILL_GRACE):
    """Capture COMMAND under the tracer. SIGTERM/SIGINT during the capture are forwarded to
    the tracer's process group (terminate_tree); outputs and receipt are still written, and
    the exit is 124 when the command's status never arrived."""
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
    if type(kill_grace) not in (int, float) or not math.isfinite(kill_grace) or kill_grace < 0:
        raise ValueError("invalid_kill_grace")
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
                   sanitized_trace_bytes=0, decoded_evidence_bytes=0, decoder_peak_bytes=0,
                   terminated_by=None, kill_grace_seconds=kill_grace,
                   tracer_stderr_tail=None, tracer_stderr_withheld=False)
    receipt_path = out / "reports" / ("trace-receipt-" + context["capture_group"] + ".json")
    state = {"process": None, "signal": None, "escalate": False, "terminated": False}
    if not facility["available"]:
        receipt["capture_errors"] = [facility["reason"]]
        receipt["command_exit_code"] = _command(command)
    else:
        manifest = build_epoch_manifest(context, facility, trace_argv)
        context.update(capture_epoch=capture_epoch(manifest), capture_epoch_manifest=manifest)
        errors = receipt["capture_errors"]

        def forward(signum, _frame):
            # The runner's timeout reached us: stop the tracer's whole group, then let
            # the capture finish on the bytes it got. A second signal skips the grace.
            if state["signal"] is not None:
                state["escalate"] = True
                if state["process"] is not None:
                    _signal_group(state["process"], KILL)
                return
            state["signal"] = signal.Signals(signum).name
            errors.append("command_timeout")
            if state["process"] is not None:
                state["terminated"] = True
                terminate_tree(state["process"], kill_grace, escalate=lambda: state["escalate"])

        with tempfile.TemporaryDirectory(prefix=".trace-", dir=out) as private:
            path = os.path.join(private, "sink")
            status_path = os.path.join(private, "command-exit.json")
            stderr_path = os.path.join(private, "tracer-stderr")
            fd = None
            if sink == "fifo":
                os.mkfifo(path, 0o600)
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            stderr_fd, token = _stderr_token()
            wrapped = [sys.executable, os.path.abspath(__file__), "_command", status_path, token] + command
            actual_argv = tracer + TRACE_FLAGS + ["-o", path, "--"] + wrapped
            receipt["tracer"]["argv"] = tracer + TRACE_FLAGS + ["-o", path, "--"]
            # Handlers live exactly around the launch and capture; removed in finally.
            previous = _install_forwarding(forward)
            try:
                process = None
                if state["signal"] is None:
                    try:
                        # Own session and group: the forwarded signal reaches every tracee.
                        # strace's stderr is private; the stderr duplicate rides to the wrapper.
                        with open(stderr_path, "wb") as tracer_stderr:
                            inherit = ({"pass_fds": (stderr_fd,)} if os.name == "posix" and stderr_fd is not None
                                       else {"close_fds": False} if stderr_fd is not None else {})
                            process = subprocess.Popen(actual_argv, stdin=subprocess.DEVNULL, stderr=tracer_stderr,
                                                       start_new_session=os.name == "posix", **inherit)
                    except OSError:
                        receipt.update(facility_available=False, facility_reason="tracer_launch_failed")
                        errors.append("tracer_launch_failed")
                        receipt["command_exit_code"] = _command(command)
                if stderr_fd is not None:
                    os.close(stderr_fd)
                    stderr_fd = None
                state["process"] = process
                if process is not None and state["signal"] is not None and not state["terminated"]:
                    # The signal landed while the tracer was being launched.
                    terminate_tree(process, kill_grace, escalate=lambda: state["escalate"])
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
                    code, restored = None, False
                    try:
                        status = json.loads(Path(status_path).read_text())
                        code, restored = status["command_exit_code"], status.get("stderr_restored") is True
                        if type(code) is not int:
                            code = None
                            raise ValueError("invalid_command_status")
                        receipt["command_exit_code"] = code
                    except (OSError, ValueError, KeyError):
                        errors.append("command_outcome_unavailable")
                        # A terminated command has no status; run() exits 124 for it.
                        receipt["command_exit_code"] = (None if state["signal"] is not None
                                                        else receipt["tracer_exit_code"] or 127)
                    # strace exits with its tracee's status: only a tracer exit that differs
                    # from the command's recorded status is the tracer's own failure.
                    if receipt["tracer_exit_code"] != 0 and receipt["tracer_exit_code"] != code:
                        errors.append("tracer_nonzero_exit")
                    # Only strace's diagnostics ("seccomp-bpf not enabled"), never tracee output:
                    # withheld unless the wrapper confirms the command wrote to our stderr.
                    if restored:
                        receipt["tracer_stderr_tail"] = _stderr_tail(stderr_path)
                    else:
                        receipt["tracer_stderr_withheld"] = True
                    receipt["terminated_by"] = state["signal"]
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
                _restore_forwarding(previous)
                if fd is not None:
                    os.close(fd)
                if stderr_fd is not None:
                    os.close(stderr_fd)
    if state["signal"] is not None:
        # Also covers a signal that landed after the outputs were built.
        receipt.update(terminated_by=state["signal"], capture_complete=False,
                       capture_errors=sorted(set(receipt["capture_errors"]) | {"command_timeout"}))
    receipt["elapsed_seconds"] = time.monotonic() - started
    tree._atomic(receipt_path, receipt)
    if receipt["command_exit_code"] is None:
        return 124
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
    launch.add_argument("--kill-grace", type=float, default=KILL_GRACE, metavar="SECONDS",
                        help="seconds between forwarding SIGTERM to the tracer's group and SIGKILL")
    # Split explicitly so a multi-token tracer cannot swallow COMMAND.
    boundary = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    if not math.isfinite(args.kill_grace) or args.kill_grace < 0:
        print("trace_supervisor: --kill-grace must be a finite, non-negative number of seconds", file=sys.stderr)
        return 2
    try:
        context = json.loads(Path(args.context).read_text(encoding="utf-8"), object_pairs_hook=tree._pairs)
        limits = {}
        for item in args.limit:
            name, value = item.split("=", 1)
            limits[name] = int(value)
        return run(context, args.out, command, args.tracer, args.sink, limits, args.descendant_grace,
                   args.suites_file, args.kill_grace)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        if args.sink == "fifo" and os.name == "nt":
            print("trace_supervisor: fifo sink is unavailable on Windows; use --sink file", file=sys.stderr)
        else:
            print("trace_supervisor: invalid context, limit, command or output", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
