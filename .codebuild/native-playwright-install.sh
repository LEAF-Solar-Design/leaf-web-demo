#!/usr/bin/env bash
# Native gate only: retry an apt mirror sync, never a browser-only install.
set -euo pipefail
exec python3 -I - "$@" <<'PY'
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile


APT_ERROR = re.compile(rb"(?:^|\n)(?:e:[ \t]*failed to fetch\b|err:[ \t]*\d+[ \t]+(?:https?|ftp)://)[^\n]*(?:\n[ \t]+[^\n]*)*")


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupt(signum, frame):
    raise Interrupted(signum)


for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, interrupt)


def stop(child):
    # Stop the installer's private process group before removing its log.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def attempt(command, log):
    child = subprocess.Popen(["timeout", "--kill-after=30s", "600s", *command],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
    mirror = False
    retained = scan = b""
    try:
        with log.open("wb") as output:
            os.chmod(log, 0o600)
            while True:
                chunk = child.stdout.read1(8192)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                # Inspect every byte, including diagnostics before a long log;
                # retain at most 64 KiB on disk and 4 KiB across read boundaries.
                scan = (scan + chunk).lower()
                # A browser checksum warning elsewhere in the output is not
                # an apt error: the signature must be in its acquire block.
                for error in APT_ERROR.finditer(scan):
                    block = error.group()
                    mirror |= b"hash sum mismatch" in block or b"mirror sync in progress" in block
                scan = scan[-4096:]
                retained = (retained + chunk)[-65536:]
                output.seek(0)
                output.write(retained)
                output.truncate()
        status = child.wait()
        status = status if status >= 0 else 128 - status
        # timeout/tool failures and signals cannot establish mirror exhaustion.
        return status, mirror and 0 < status < 124
    finally:
        child.stdout.close()
        stop(child)


def main():
    if len(sys.argv) < 2:
        return 2
    with tempfile.TemporaryDirectory(prefix="leaf-native-playwright-") as work:
        log = Path(work) / "install.log"
        status, mirror = attempt(sys.argv[1:], log)
        if not mirror:
            return status
        delay = subprocess.Popen(["sleep", "5"], start_new_session=True)
        try:
            delay_status = delay.wait()
            if delay_status:
                return delay_status if delay_status > 0 else 128 - delay_status
        finally:
            stop(delay)
        status, mirror = attempt(sys.argv[1:], log)
        if mirror:
            print("infra_unavailable: apt_mirror_sync_exhausted", file=sys.stderr)
            return 75
        return status


try:
    sys.exit(main())
except Interrupted as error:
    sys.exit(128 + error.signum)
PY
