#!/usr/bin/env python3
"""Opt-in selected synthetic Next proof. Invoke through canonical offhost admission.

No installation or shared cache writes. Only the sanitized receipt is printed.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import struct
import sys
import tempfile
import time

sys.dont_write_bytecode = True

from qualify_linux_browser_overlay import (
    BROWSER, LOCK_HASH, MIB, PLAYWRIGHT, QualificationError, Supervisor,
    Unavailable, digest, prepare_overlay, require,
)


def port_free(port):
    # Binding also rejects a listener that does not answer HTTP. Never kill it.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def package_identity(modules, lock):
    identities = {}
    for name in ('playwright', 'playwright-core', '@playwright/test', 'next',
                 '@next/env', '@next/swc-linux-x64-gnu', 'react', 'react-dom'):
        package = modules / name / 'package.json'
        data = json.loads(package.read_text(encoding='utf-8'))
        expected = lock['packages']['node_modules/' + name]['version']
        require(data.get('name') == name and data.get('version') == expected,
                'cache-version-mismatch')
        if name in ('playwright', 'playwright-core', '@playwright/test'):
            require(expected == '1.60.0', 'playwright-version-mismatch')
        identities[name] = {'version': expected, 'sha256': digest(package)}
    return identities


def snapshot(repo, destination, deadline):
    """Keep Next's generated files and any fixture writes inside our root."""
    total = 0
    manifest = []

    def copy(source, target):
        nonlocal total
        require(time.monotonic() < deadline, 'workflow-timeout')
        origin = Path(source)
        require(origin.resolve().is_relative_to(repo), 'source-link-escape')
        total += origin.stat().st_size
        require(total <= 512 * MIB, 'source-size-limit')
        result = shutil.copy2(source, target)
        manifest.append((origin.relative_to(repo).as_posix(), digest(Path(target))))
        return result

    def ignore(directory, names):
        require(time.monotonic() < deadline, 'workflow-timeout')
        excluded = {'.git', 'node_modules', 'artifacts', 'dist', '__pycache__',
                    '.pytest_cache', '.venv', 'venv', 'test-results', 'playwright-report'}
        result = []
        for name in names:
            entry = Path(directory) / name
            if (name in excluded or name.startswith(('.env', '.next', 'studio-application-e2e-'))):
                result.append(name)
            else:
                require(not entry.is_symlink(), 'source-symlink-unsupported')
        return result

    shutil.copytree(repo, destination, copy_function=copy, ignore=ignore)
    encoded = json.dumps(sorted(manifest), separators=(',', ':')).encode('utf-8')
    return {'sha256': hashlib.sha256(encoded).hexdigest(), 'files': len(manifest),
            'bytes': total, 'format': 'sorted-relative-path-sha256-json-v1'}


def proof(report_path, root, receipt):
    require(report_path.is_file() and report_path.stat().st_size <= 8 * MIB,
            'report-unavailable')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    counts = {key: report.get('stats', {}).get(key) for key in
              ('expected', 'unexpected', 'flaky', 'skipped')}
    cases = []

    def visit(suite):
        for spec in suite.get('specs', []):
            cases.extend(spec.get('tests', []))
        for child in suite.get('suites', []):
            visit(child)

    for suite in report.get('suites', []):
        visit(suite)
    counts['total'] = len(cases)
    counts['passed'] = sum(test.get('status') == 'expected' and
                           test.get('expectedStatus') == 'passed' and
                           len(test.get('results', [])) == 1 and
                           test['results'][0].get('status') == 'passed' for test in cases)
    receipt['test_counts'] = counts
    require(counts == {'expected': 1, 'unexpected': 0, 'flaky': 0,
                       'skipped': 0, 'total': 1, 'passed': 1}
            and not report.get('errors'), 'selected-case-not-passed')
    case = cases[0]
    require(case.get('projectName') == 'linux-selected', 'selected-project-mismatch')
    attachments = [item for item in case['results'][0].get('attachments', [])
                   if item.get('name') == 'screenshot' and item.get('contentType') == 'image/png']
    require(len(attachments) == 1, 'screenshot-count-mismatch')
    image = Path(attachments[0].get('path', '')).resolve()
    require(image.is_relative_to(root / 'test-results') and image.is_file()
            and 24 <= image.stat().st_size <= (2 * MIB // 4) * 3,
            'screenshot-size-or-path')
    data = image.read_bytes()
    require(data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR'
            and data[8:12] == b'\x00\x00\x00\x0d', 'screenshot-invalid-png')
    width, height = struct.unpack('>II', data[16:24])
    require(width > 0 and height > 0, 'screenshot-invalid-dimensions')
    receipt['screenshot'] = {'sha256': digest(image), 'width': width, 'height': height,
                             'png_base64': base64.b64encode(data).decode('ascii')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, help='Exact repo-relative web/e2e/*.spec.mjs or .spec.js')
    parser.add_argument('--grep', required=True, help='Exact Playwright full title (including project/file)')
    parser.add_argument('--source', required=True, help='Canonical supplied 40-hex source identity')
    parser.add_argument('--worker', type=int, choices=range(8), default=0)
    args = parser.parse_args()
    started = time.monotonic()
    deadline = started + 580  # Reserve 20 seconds for inherited extinction and cleanup.
    receipt = {'outcome': 'failed', 'source': None, 'spec': None,
               'spec_sha256': None, 'lock_sha256': None, 'input_sha256': {},
               'source_snapshot': None, 'package_identity': {},
               'browser_version': None, 'browser_sha256': None,
               'selected_test_title': None, 'port': 5185 + 100 * args.worker,
               'browser_revision': '1223', 'test_counts': None, 'screenshot': None,
               'owned_process_extinction': False, 'own_link_cleanup': False,
               'owned_temp_cleanup': False, 'port_free_after_exit': None}
    temporary = root = runner = link = link_identity = root_identity = None
    port_admitted = False
    try:
        require(bool(re.fullmatch(r'[0-9a-f]{40}', args.source)), 'invalid-source')
        receipt['source'] = args.source
        require(0 < len(args.grep) <= 1024 and
                not re.search(r'[\x00-\x1f\x7f]', args.grep), 'invalid-title')
        receipt['selected_test_title'] = args.grep
        relative = PurePosixPath(args.spec)
        require(len(args.spec) <= 512 and not relative.is_absolute()
                and '..' not in relative.parts and '.' not in args.spec.split('/')
                and re.fullmatch(r'web/e2e/(?:[^/]+/)*[^/]+\.spec\.(?:mjs|js)', args.spec)
                and '\\' not in args.spec and not re.search(r'[\x00-\x1f\x7f]', args.spec),
                'invalid-spec')
        repo = Path.cwd().resolve()
        selected = (repo / args.spec).resolve()
        require(selected.is_relative_to(repo / 'web/e2e') and selected.is_file(),
                'spec-missing-or-escape')
        receipt['spec'] = args.spec
        receipt['spec_sha256'] = digest(selected)
        require(sys.platform == 'linux' and platform.machine() == 'x86_64'
                and os.getuid() == 1001, 'unsupported-runtime')
        release = Path('/etc/os-release').read_text(encoding='utf-8')
        require(re.search(r'^ID=(?:"debian"|debian)$', release, re.M)
                and re.search(r'^VERSION_ID=(?:"13"|13)$', release, re.M),
                'unsupported-distribution')
        lockfile = repo / 'web/package-lock.json'
        receipt['lock_sha256'] = digest(lockfile)
        require(receipt['lock_sha256'] == LOCK_HASH, 'lock-hash-mismatch')
        lock = json.loads(lockfile.read_text(encoding='utf-8'))
        modules = PLAYWRIGHT.parent
        receipt['package_identity'] = package_identity(modules, lock)
        existing = repo / 'web/node_modules'
        if existing.exists() or existing.is_symlink():
            package_identity(existing, lock)  # Never replace or delete caller's modules.
        browsers = json.loads((modules / 'playwright-core/browsers.json').read_text())
        browser = [b for b in browsers['browsers'] if b['name'] == 'chromium-headless-shell']
        require(len(browser) == 1 and browser[0]['revision'] == '1223'
                and browser[0]['browserVersion'] == '148.0.7778.96', 'browser-cache-mismatch')
        receipt['browser_version'] = browser[0]['browserVersion']
        require(BROWSER.is_file() and os.access(BROWSER, os.X_OK), 'browser-unavailable')
        with BROWSER.open('rb') as stream:
            require(stream.read(4) == b'\x7fELF', 'browser-not-elf')
        receipt['browser_sha256'] = digest(BROWSER)
        require(port_free(receipt['port']), 'selected-port-occupied')
        port_admitted = True
        temporary = tempfile.TemporaryDirectory(prefix='studio-application-e2e-', dir=repo)
        root = Path(temporary.name).resolve()
        root_identity = (root.stat().st_dev, root.stat().st_ino)
        require(root.parent == repo, 'invalid-owned-root')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
        for key, name in (('HOME', 'home'), ('TMPDIR', 'tmp'), ('XDG_CACHE_HOME', 'cache'),
                          ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                          ('XDG_RUNTIME_DIR', 'runtime')):
            directory = root / name
            directory.mkdir(mode=0o700)
            env[key] = str(directory)
        runner = Supervisor(root, deadline, env)
        require(ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0,
                'subreaper-unavailable')
        child_env = prepare_overlay(root, runner, receipt)
        actual_version = runner.run([str(BROWSER), '--version'], 'browser-version',
                                    env=child_env).decode('ascii', 'strict').strip()
        require(re.fullmatch(r'(?:Chromium|Google Chrome for Testing|HeadlessChrome) '
                             r'148\.0\.7778\.96', actual_version),
                'browser-version-mismatch')
        staged = root / 'source'
        receipt['source_snapshot'] = snapshot(repo, staged, deadline)
        require(digest(staged / args.spec) == receipt['spec_sha256']
                and digest(staged / 'web/package-lock.json') == LOCK_HASH,
                'source-copy-mismatch')
        receipt['input_sha256'] = {name: digest(staged / name) for name in (
            args.spec, 'web/package-lock.json', 'web/package.json',
            'web/playwright.config.mjs', 'web/playwright.linux-selected.config.mjs',
            'web/next.config.mjs', 'web/scripts/next-dev.mjs')}
        link = staged / 'web/node_modules'
        link.symlink_to(modules, target_is_directory=True)
        link_identity = (link.lstat().st_dev, link.lstat().st_ino)
        child_env.update({'LEAF_NATIVE_GATE_WORKER': str(args.worker),
                          'LEAF_LINUX_APPLICATION_ROOT': str(root),
                          'LEAF_LINUX_APPLICATION_SPEC': args.spec,
                          'LEAF_LINUX_APPLICATION_TITLE': args.grep,
                          'LEAF_LINUX_APPLICATION_BROWSER': str(BROWSER),
                          'NEXT_TELEMETRY_DISABLED': '1', 'LEAF_SOURCE_SHA': args.source,
                          'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1'})
        require(port_free(receipt['port']), 'selected-port-occupied')
        runner.run(['node', str(modules / '@playwright/test/cli.js'), 'test',
                    '--config', 'playwright.linux-selected.config.mjs'],
                   'selected-application', timeout=360, env=child_env,
                   cwd=staged / 'web', allow_failure=True)
        proof(root / 'report.json', root, receipt)
        require(runner.last_returncode == 0, 'selected-application-failed')
        receipt['outcome'] = 'passed'
    except Unavailable as error:
        receipt['outcome'] = 'unavailable'
        receipt['error'] = str(error)
    except QualificationError as error:
        receipt['error'] = str(error)
    except Exception:
        receipt['error'] = 'application-adapter-error'
    finally:
        receipt['timings'] = runner.timings if runner else []
        try:
            require(runner is None or (not runner.groups and not runner.census()),
                    'owned-process-extinction-unconfirmed')
            receipt['owned_process_extinction'] = True
            if port_admitted:
                receipt['port_free_after_exit'] = port_free(receipt['port'])
            if link_identity is not None:
                require(link.is_symlink() and
                        (link.lstat().st_dev, link.lstat().st_ino) == link_identity
                        and link.resolve() == PLAYWRIGHT.parent.resolve(), 'cleanup-link-mismatch')
                link.unlink()
            receipt['own_link_cleanup'] = True
            if temporary:
                require(not root.is_symlink() and root.parent == Path.cwd().resolve()
                        and root.name.startswith('studio-application-e2e-')
                        and (root.stat().st_dev, root.stat().st_ino) == root_identity,
                        'cleanup-root-mismatch')
                temporary.cleanup()
                require(not root.exists(), 'cleanup-root-remains')
            receipt['owned_temp_cleanup'] = True
            require(not port_admitted or receipt['port_free_after_exit'],
                    'cleanup-port-unconfirmed')
        except Exception:
            if temporary:
                temporary._finalizer.detach()
            receipt['error'] = 'cleanup-unconfirmed'
            receipt['outcome'] = 'failed'
        receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
        print(json.dumps(receipt, separators=(',', ':'), sort_keys=True))
    return 0 if receipt['outcome'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
