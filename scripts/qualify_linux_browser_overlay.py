#!/usr/bin/env python3
"""Opt-in, disposable Debian library qualification; never installs packages.

Run only through the offhost verify route. The receipt describes this process's
private directory cleanup, not cleanup of the runner or its queue job.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time


LOCK_HASH = "e80745e581c48c1f2cf37e8788f7d6dd3f550f5b8d96a58831f843830458bd02"
PLAYWRIGHT = Path("/home/offhost-seat/offhost/envs/node-eddc10892fc12983c266f6aecf475a04/node_modules/playwright")
BROWSER = Path("/home/offhost-seat/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell")
PACKAGES = {
    "libnspr4": "2:4.36-1",
    "libnss3": "2:3.110-1+deb13u4",
    "libatk1.0-0t64": "2.56.2-1+deb13u2",
    "libatk-bridge2.0-0t64": "2.56.2-1+deb13u2",
    "libxdamage1": "1:1.1.6-1+b2",
    "libxkbcommon0": "1.7.0-2",
    "libasound2t64": "1.2.14-1+deb13u1",
    "libatspi2.0-0t64": "2.56.2-1+deb13u2",
    "at-spi2-common": "2.56.2-1+deb13u2",
    "libasound2-data": "1.2.14-1+deb13u1",
    "xkb-data": "2.42-1",
}
SONAMES = (
    "libnspr4.so", "libnss3.so", "libnssutil3.so", "libatspi.so.0",
    "libatk-1.0.so.0", "libatk-bridge-2.0.so.0", "libXdamage.so.1",
    "libxkbcommon.so.0", "libasound.so.2",
)
MIB = 1024 * 1024
DOWNLOAD_LIMIT = 64 * MIB
EXPANDED_LIMIT = 256 * MIB


class QualificationError(Exception):
    pass


class Unavailable(QualificationError):
    pass


def require(condition, code):
    if not condition:
        raise QualificationError(code)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            result.update(block)
    return result.hexdigest()


class Supervisor:
    """Bound output/time and retire only process groups created by this tool."""

    def __init__(self, root, deadline, env):
        self.root = root
        self.deadline = deadline
        self.env = env
        self.groups = set()
        self.descendants = {}
        self.timings = []
        self.last_returncode = None

    def census(self):
        # Chromium is spawned detached by Playwright. Session/group membership
        # alone is therefore insufficient. Subreaping preserves ancestry even
        # if Node exits before its browser; start ticks protect against PID reuse.
        processes = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "stat").read_text()
                fields = raw[raw.rfind(")") + 2:].split()
                processes[int(entry.name)] = (int(fields[1]), fields[19])
            except (FileNotFoundError, ProcessLookupError):
                continue
        owned = {os.getpid()}
        owned.update(pid for pid, ticks in self.descendants.items()
                     if pid in processes and processes[pid][1] == ticks)
        changed = True
        while changed:
            changed = False
            for pid, (parent, ticks) in processes.items():
                if parent in owned and pid not in owned:
                    owned.add(pid)
                    changed = True
        self.descendants = {pid: processes[pid][1] for pid in owned
                            if pid != os.getpid() and pid in processes}
        return self.descendants

    @staticmethod
    def alive(group):
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False

    def retire(self, child):
        # Playwright normally closes the browser itself. This also handles
        # descendants left behind by an exited/timed-out group leader.
        self.census()
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pid in self.census():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=3)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            # Reap only our adopted children, after Popen has reaped its leader.
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        break
                except ChildProcessError:
                    break
            remaining = self.census()
            if not remaining and not self.alive(child.pid):
                break
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.02)
        require(not self.census() and not self.alive(child.pid),
                "child-group-extinction-unconfirmed")
        self.groups.discard(child.pid)

    def run(self, argv, label, *, timeout=15, env=None, cwd=None,
            output_path=None, output_limit=MIB, storage_limit=None,
            allow_failure=False):
        started = time.monotonic()
        until = min(self.deadline, started + timeout)
        require(started < until, "workflow-timeout")
        out = bytearray()
        err = bytearray()
        count = 0
        sink = output_path.open("wb") if output_path else None
        child = None
        try:
            child = subprocess.Popen(
                argv, cwd=cwd or self.root, env=env or self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
            self.groups.add(child.pid)
            with selectors.DefaultSelector() as selector:
                for pipe in (child.stdout, child.stderr):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map() or child.poll() is None:
                    if time.monotonic() >= until:
                        if label.startswith(("metadata-", "download-")):
                            raise Unavailable(label + "-timeout")
                        raise QualificationError(label + "-timeout")
                    self.census()
                    if storage_limit is not None:
                        size = sum(p.stat().st_size for p in Path(cwd).iterdir()
                                   if p.is_file())
                        require(size <= storage_limit, "download-size-limit")
                    for key, _ in selector.select(0.05):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if key.fileobj is child.stdout:
                            count += len(data)
                            require(count <= output_limit, label + "-stdout-limit")
                            if sink:
                                sink.write(data)
                            else:
                                out.extend(data)
                        else:
                            require(len(err) + len(data) <= MIB, label + "-stderr-limit")
                            err.extend(data)
            code = child.wait(timeout=1)
            self.last_returncode = code
            # Diagnostics are intentionally not copied into the final receipt:
            # apt/proxy and inherited-environment errors can contain secrets.
            if code and not allow_failure:
                if label.startswith(("metadata-", "download-")):
                    raise Unavailable(label + "-unavailable")
                raise QualificationError(label + "-exit-" + str(code))
            return bytes(out)
        finally:
            if sink:
                sink.close()
            if child is not None:
                try:
                    self.retire(child)
                finally:
                    child.stdout.close()
                    child.stderr.close()
            self.timings.append({"command": label,
                                 "seconds": round(time.monotonic() - started, 3)})


def records(data):
    """Parse Debian control paragraphs, including continuation lines."""
    result = []
    if not data.strip():
        return result
    for paragraph in data.decode("utf-8", "strict").strip().split("\n\n"):
        fields = {}
        previous = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")) and previous:
                fields[previous] += "\n" + line
            else:
                key, sep, value = line.partition(":")
                require(bool(sep) and key not in fields, "invalid-package-metadata")
                fields[key] = value.strip()
                previous = key
        result.append(fields)
    return result


def inspect_archive(path, overlay, expanded, deadline):
    """Inspect the complete filesystem tar before dpkg-deb may extract it.

    Resolve both earlier-package links and links declared later in this tar.
    Hard links name archive-root paths; symlinks name parent-relative paths.
    """
    links = {}
    for existing in overlay.rglob("*"):
        require(time.monotonic() < deadline, "workflow-timeout")
        if existing.is_symlink():
            name = PurePosixPath(existing.relative_to(overlay).as_posix())
            target = PurePosixPath(os.readlink(existing))
            require(not target.is_absolute(), "absolute-existing-link")
            links[str(name)] = name.parent / target
    members = []
    with tarfile.open(path, "r:") as archive:
        for item in archive:
            require(time.monotonic() < deadline, "workflow-timeout")
            require(len(members) < 50000, "archive-member-limit")
            name = PurePosixPath(item.name)
            require(not name.is_absolute() and ".." not in name.parts,
                    "unsafe-archive-path")
            require(bool(name.parts) or item.isdir(), "unsafe-archive-root")
            require(item.isdir() or item.isfile() or item.issym() or item.islnk(),
                    "unsafe-archive-type")
            require(item.size >= 0, "invalid-archive-size")
            expanded += item.size
            require(expanded <= EXPANDED_LIMIT, "expanded-size-limit")
            require(not (item.mode & 0o6000), "privileged-archive-mode")
            if item.issym() or item.islnk():
                target = PurePosixPath(item.linkname)
                require(not target.is_absolute(), "absolute-archive-link")
                target = name.parent / target if item.issym() else target
                require(str(name) not in links, "duplicate-archive-link")
                links[str(name)] = target
            members.append((name, item.isdir(), item.issym() or item.islnk()))

    def resolve(name, seen=None):
        seen = set() if seen is None else seen
        parts = []
        for part in name.parts:
            if part == ".":
                continue
            if part == "..":
                require(bool(parts), "escaping-archive-link")
                parts.pop()
                continue
            parts.append(part)
            key = str(PurePosixPath(*parts))
            if key in links:
                require(key not in seen and len(seen) < 40, "cyclic-archive-link")
                parts = list(resolve(links[key], seen | {key}).parts)
        candidate = PurePosixPath(*parts)
        require((overlay / str(candidate)).resolve().is_relative_to(overlay),
                "escaping-existing-link")
        return candidate

    for name, directory, link in members:
        resolve(name)
        # Never let a regular file replace a link from an earlier package.
        destination = overlay / str(name)
        require(not destination.is_symlink() or link, "archive-overwrites-link")
        if not directory and not link:
            require(not destination.exists(), "archive-file-collision")
    return expanded


NODE_SCRIPT = r"""
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require(process.argv[2]);
const root = process.argv[4];
const result = {launch: false, content: false, screenshot: false, close: false};
(async () => {
  let context;
  let browser;
  let disconnected = false;
  let failed = false;
  try {
    context = await chromium.launchPersistentContext(path.join(root, 'profile'), {
      executablePath: process.argv[3], headless: true, chromiumSandbox: true,
      viewport: {width: 390, height: 844}, timeout: 25000,
      env: {...process.env},
    });
    result.launch = true;
    browser = context.browser();
    browser.on('disconnected', () => { disconnected = true; });
    result.browser_version = browser.version();
    await context.route('**/*', route => route.abort());
    const page = context.pages()[0] || await context.newPage();
    await page.goto('about:blank', {timeout: 5000});
    await page.setContent('<!doctype html><meta charset="utf-8"><h1>Studio browser qualification</h1>', {timeout: 5000});
    const heading = await page.locator('h1').textContent({timeout: 5000});
    const width = await page.evaluate(() => window.innerWidth);
    if (heading !== 'Studio browser qualification' || width !== 390)
      throw new Error('content-check');
    result.heading = heading;
    result.viewport_width = width;
    result.content = true;
    const image = await page.screenshot({path: path.join(root, 'qualification.png'), timeout: 10000});
    if (!image.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ||
        image.toString('ascii', 12, 16) !== 'IHDR') throw new Error('png-check');
    result.image = {sha256: crypto.createHash('sha256').update(image).digest('hex'),
      width: image.readUInt32BE(16), height: image.readUInt32BE(20)};
    if (result.image.width !== 390 || result.image.height !== 844) throw new Error('png-size');
    result.screenshot = true;
  } catch (_) {
    failed = true;
    result.error = 'browser-qualification-failed';
  } finally {
    if (context) {
      try {
        await context.close();
        result.close = disconnected && !browser.isConnected();
      } catch (_) { result.close = false; }
    }
    console.log(JSON.stringify(result));
    if (failed || !result.close) process.exitCode = 1;
  }
})();
"""


def prepare_overlay(root, runner, receipt):
    """Prepare and validate private libraries without launching a browser."""
    overlay = root / "overlay"
    downloads = root / "downloads"
    overlay.mkdir()
    downloads.mkdir()
    metadata = []
    total = 0
    for name, version in PACKAGES.items():
        output = runner.run(["apt-cache", "-o", "Dir::Cache=" + str(root / "apt-cache"),
                             "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
                             "show", name + "=" + version],
                            "metadata-" + name, timeout=5)
        matches = [r for r in records(output) if r.get("Package") == name
                   and r.get("Version") == version
                   and r.get("Architecture") in ("amd64", "all")]
        if len(matches) != 1:
            raise Unavailable("metadata-not-unique-" + name)
        record = matches[0]
        require(bool(re.fullmatch(r"[0-9a-f]{64}", record.get("SHA256", ""))),
                "invalid-package-sha256")
        require(record.get("Size", "").isdigit(), "invalid-package-size")
        size = int(record["Size"])
        require(size > 0, "invalid-package-size")
        total += size
        require(total <= DOWNLOAD_LIMIT, "download-size-limit")
        metadata.append({"package": name, "version": version,
                         "architecture": record["Architecture"],
                         "sha256": record["SHA256"], "size": size})

    # One attempt, exact versions, existing metadata, no install/update/cache.
    argv = ["apt-get", "-o", "Debug::NoLocking=true",
            "-o", "Dir::Cache=" + str(root / "apt-cache"),
            "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
            "-o", "Dir::Log=" + str(root / "apt-log"),
            "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=15",
            "-o", "Acquire::https::Timeout=15", "download"]
    argv += [r["package"] + (":amd64" if r["architecture"] == "amd64" else "")
             + "=" + r["version"] for r in metadata]
    runner.run(argv, "download-packages", timeout=50, cwd=downloads,
               storage_limit=DOWNLOAD_LIMIT)
    files = list(downloads.iterdir())
    require(len(files) == len(metadata) and all(p.is_file() and not p.is_symlink()
                                               for p in files), "download-file-set")
    require(sum(p.stat().st_size for p in files) <= DOWNLOAD_LIMIT, "download-size-limit")
    expected = {r["sha256"]: r for r in metadata}
    require(len(expected) == len(metadata), "duplicate-package-digest")
    verified = []
    for package in files:
        sha = digest(package)
        require(sha in expected, "download-hash-mismatch")
        record = expected.pop(sha)
        require(package.stat().st_size == record["size"], "download-size-mismatch")
        fields = runner.run(["dpkg-deb", "--field", str(package), "Package",
                             "Version", "Architecture"], "fields-" + record["package"])
        parsed = records(fields)[0]
        require(all(parsed.get(key) == record[key.lower()] for key in
                    ("Package", "Version", "Architecture")), "package-fields-mismatch")
        verified.append((package, record))
    require(not expected, "missing-downloaded-package")
    receipt["packages"] = [record for _, record in verified]
    receipt["download_bytes"] = total
    expanded = 0
    for package, record in verified:
        archive = root / "filesystem.tar"
        runner.run(["dpkg-deb", "--fsys-tarfile", str(package)],
                   "tar-" + record["package"], output_path=archive,
                   output_limit=EXPANDED_LIMIT + 16 * MIB)
        expanded = inspect_archive(archive, overlay, expanded, runner.deadline)
        runner.run(["dpkg-deb", "--extract", str(package), str(overlay)],
                   "extract-" + record["package"])
        archive.unlink()
    receipt["expanded_bytes"] = expanded
    lib = overlay / "usr/lib/x86_64-linux-gnu"
    receipt["missing_libraries"] = [name for name in SONAMES
                                    if not (lib / name).is_file()]
    require(not receipt["missing_libraries"], "missing-overlay-sonames")
    child_env = dict(runner.env, LD_LIBRARY_PATH=str(lib))
    ldd = runner.run(["ldd", str(BROWSER)], "ldd", env=child_env).decode("utf-8", "strict")
    receipt["missing_libraries"] = re.findall(r"^\s*(\S+)\s+=>\s+not found", ldd, re.M)
    require(not receipt["missing_libraries"] and "not found" not in ldd, "ldd-unresolved")
    # Confirm the nine prerequisites actually resolve from this overlay.
    for name in SONAMES:
        match = re.search(r"^\s*" + re.escape(name) + r"\s+=>\s+(\S+)", ldd, re.M)
        require(match is not None and Path(match[1]).resolve().is_relative_to(overlay),
                "ldd-overlay-resolution")
    child_env["XKB_CONFIG_ROOT"] = str(overlay / "usr/share/X11/xkb")
    child_env["ALSA_CONFIG_PATH"] = str(overlay / "usr/share/alsa/alsa.conf")
    child_env["ALSA_CONFIG_DIR"] = str(overlay / "usr/share/alsa")
    return child_env


def qualify(root, runner, receipt):
    child_env = prepare_overlay(root, runner, receipt)
    script = root / "qualify.cjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    output = runner.run(["node", str(script), str(PLAYWRIGHT), str(BROWSER), str(root)],
                        "browser", timeout=50, env=child_env, allow_failure=True)
    result = json.loads(output)
    receipt["browser"] = result
    require(all(result.get(key) is True for key in ("launch", "content", "screenshot", "close")),
            "incomplete-browser-proof")
    image = root / "qualification.png"
    require(image.is_file() and image.stat().st_size <= 8 * MIB, "invalid-image-size")
    with image.open("rb") as stream:
        header = stream.read(24)
    require(len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n"
            and header[12:16] == b"IHDR", "invalid-png")
    width, height = struct.unpack(">II", header[16:24])
    require(width == 390 and height == 844 and
            result["image"] == {"sha256": digest(image), "width": width, "height": height},
            "image-proof-mismatch")


def main():
    started = time.monotonic()
    # Reserve cleanup time; useful execution is strictly below 180 seconds.
    deadline = started + 165
    receipt = {"status": "failed", "lock_sha256": None,
               "browser_revision": "1223", "playwright_version": None,
               "browser_executable": str(BROWSER),
               "playwright_package": str(PLAYWRIGHT / "package.json"),
               "packages": [], "missing_libraries": [],
               "cleanup_complete": False}
    temporary = None
    root = None
    runner = None
    try:
        require(sys.platform == "linux" and platform.machine() == "x86_64"
                and os.getuid() == 1001, "unsupported-runtime")
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
        require(re.search(r'^ID=(?:"debian"|debian)$', os_release, re.M) is not None
                and re.search(r'^VERSION_ID=(?:"13"|13)$', os_release, re.M) is not None,
                "unsupported-distribution")
        cwd = Path.cwd().resolve()
        receipt["lock_sha256"] = digest(cwd / "web/package-lock.json")
        require(receipt["lock_sha256"] == LOCK_HASH, "lock-hash-mismatch")
        package = json.loads((PLAYWRIGHT / "package.json").read_text(encoding="utf-8"))
        receipt["playwright_version"] = package.get("version")
        require(package.get("name") == "playwright" and package.get("version") == "1.60.0",
                "playwright-version-mismatch")
        require(BROWSER.is_file() and os.access(BROWSER, os.X_OK), "browser-unavailable")
        with BROWSER.open("rb") as stream:
            require(stream.read(4) == b"\x7fELF", "browser-not-elf")
        temporary = tempfile.TemporaryDirectory(prefix="studio-browser-overlay-", dir=cwd)
        root = Path(temporary.name).resolve()
        require(root.parent == cwd and root.name.startswith("studio-browser-overlay-"),
                "invalid-owned-root")
        # An allowlist avoids passing credentials, Node hooks or loader injection
        # to the browser. No os.environ mutations and no shared HOME writes.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "LC_ALL": "C.UTF-8"}
        for key, subdir in (("HOME", "home"), ("TMPDIR", "tmp"),
                            ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config"),
                            ("XDG_DATA_HOME", "data"), ("XDG_RUNTIME_DIR", "runtime")):
            directory = root / subdir
            directory.mkdir(mode=0o700)
            env[key] = str(directory)
        runner = Supervisor(root, deadline, env)
        # PR_SET_CHILD_SUBREAPER: detached browser descendants become our children
        # on Node exit, so no orphan can outlive the owned directory's removal.
        require(ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0,
                "subreaper-unavailable")
        qualify(root, runner, receipt)
        receipt["status"] = "passed"
    except Unavailable as error:
        receipt["status"] = "unavailable"
        receipt["error"] = str(error)
    except QualificationError as error:
        receipt["error"] = str(error)
    except Exception as error:
        receipt["error"] = "unexpected-" + type(error).__name__
    finally:
        if runner:
            receipt["timings"] = runner.timings
        try:
            require(runner is None or (not runner.groups and not runner.census()),
                    "live-owned-child-groups")
            if temporary:
                require(root == Path(temporary.name).resolve()
                        and root.parent == Path.cwd().resolve()
                        and not Path(temporary.name).is_symlink(), "cleanup-root-mismatch")
                temporary.cleanup()
                receipt["cleanup_complete"] = not root.exists()
            else:
                receipt["cleanup_complete"] = True  # No private root was created.
        except Exception:
            # Do not remove a tree while any owned child could still use it.
            # Disable TemporaryDirectory's implicit finalizer in that case too.
            if temporary:
                temporary._finalizer.detach()
            receipt["cleanup_complete"] = False
            receipt["error"] = "cleanup-unconfirmed"
        if not receipt["cleanup_complete"]:
            receipt["status"] = "failed"
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return 0 if receipt["status"] == "passed" and receipt["cleanup_complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
