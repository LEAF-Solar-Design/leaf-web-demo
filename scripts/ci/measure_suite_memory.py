#!/usr/bin/env python3
"""Sample a Linux command's process-tree RSS; no shared runner integration."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


CAVEATS = [
    "Summed RSS can double count shared pages.",
    "Sampling can miss sub-interval spikes and descendants that exit between scans.",
    "Process-tree scans are not atomic; inaccessible or vanished processes are skipped.",
    "Sampled demand is not a cgroup peak or safe worker admission proof.",
]


def positive_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def suite_label(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise argparse.ArgumentTypeError(
            "suite must be 1-128 ASCII letters, digits, underscores, dots, colons or hyphens"
        )
    return value


def read_stat(pid):
    """Return (ppid, starttime); comm may contain whitespace and parentheses."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()  # fields start at state (3)
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


class TreeSampler:
    def __init__(self, pid):
        stat = read_stat(pid)
        self.known = {(pid, stat[1])} if stat is not None else set()
        self.page_size = os.sysconf("SC_PAGE_SIZE")

    def sample(self):
        snapshot = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                pid = int(entry.name)
                stat = read_stat(pid)
                if stat is not None:
                    snapshot[pid] = stat
        live = {
            pid: start for pid, start in self.known
            if pid in snapshot and snapshot[pid][1] == start
        }
        # Expand to arbitrary depth, independent of /proc directory ordering.
        while True:
            added = {}
            for pid, (ppid, start) in snapshot.items():
                if pid in live or ppid not in live or start < live[ppid]:
                    continue
                # A recycled numeric parent PID must not establish ancestry.
                if read_stat(ppid) == snapshot[ppid]:
                    added[pid] = start
            if not added:
                break
            live.update(added)
        self.known = set(live.items())
        total = 0
        for pid, start in live.items():
            try:
                before = read_stat(pid)
                if before is None or before[1] != start:
                    continue
                resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
                after = read_stat(pid)
                if after is not None and after[1] == start:
                    total += resident * self.page_size
            except (OSError, ValueError, IndexError):
                continue
        return total


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
        return True
    except ProcessLookupError:
        return False


def stop_group(child):
    """Only target the group created by start_new_session, including survivors."""
    pgid = child.pid
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not signal_group(pgid, 0):
            break
        # Do not reap the leader yet: its PID anchors the recorded group ID.
        time.sleep(0.01)
    signal_group(pgid, signal.SIGKILL)
    child.wait(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=suite_label)
    parser.add_argument("--timeout", required=True, type=positive_seconds)
    parser.add_argument("--interval", "--sample-interval", default=0.02,
                        type=positive_seconds, help="sampling interval in seconds (default: 0.02)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if sys.platform != "linux" or not Path("/proc/self/statm").is_file():
        parser.error("suite memory measurement requires Linux with /proc mounted")

    interrupted = 0

    def on_signal(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    child = None
    started = time.monotonic()
    peak = samples = 0
    timed_out = False
    try:
        try:
            child = subprocess.Popen(command, start_new_session=True)
        except OSError:
            # Do not echo command arguments or environment, even on spawn failure.
            print("suite memory: could not start command", file=sys.stderr)
            return 127
        sampler = TreeSampler(child.pid)
        while True:
            peak = max(peak, sampler.sample())
            samples += 1
            if interrupted:
                stop_group(child)
                break
            if child.poll() is not None:
                break
            remaining = args.timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                stop_group(child)
                break
            next_sample = time.monotonic() + min(args.interval, remaining)
            while not interrupted:
                delay = next_sample - time.monotonic()
                if delay <= 0:
                    break
                # Long requested intervals must not delay interrupt cleanup.
                time.sleep(min(delay, 0.05))
        exit_code = child.returncode
        record = {
            "schema": "leaf.ci.suite-memory.v1",
            "suite": args.suite,
            "exit_code": exit_code,
            "sampled_peak_rss_bytes": peak,
            "sample_count": samples,
            "elapsed_seconds": time.monotonic() - started,
            "timed_out": timed_out,
            "platform": sys.platform,
            "measurement_caveats": CAVEATS,
        }
        # Start a new line even when the command ended with unterminated output.
        print("\nLEAF_SUITE_MEMORY " + json.dumps(record, sort_keys=True), flush=True)
        if timed_out:
            return 124
        if interrupted:
            return 128 + interrupted
        return exit_code if exit_code >= 0 else 128 - exit_code
    finally:
        if child is not None and child.returncode is None:
            stop_group(child)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
