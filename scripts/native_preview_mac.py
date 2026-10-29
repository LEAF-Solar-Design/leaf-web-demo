"""Fixed canonical Mac adapter; imports no allocator until actual execution."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time

from native_preview_worker import WorkerError, require, canonical_uuid, parse, validate_png

CLIENT_DIR = Path('C:/Users/ehaug/.claude/scripts/mac-mini')
MAX_TRANSFER = 4 * ((16 * 1024 * 1024 + 2) // 3) + 16384
BUNDLE = 'com.example.portfolio'


class QuietDiagnostics:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def supervise(argv, permit, *, input_bytes=None, clock=time.monotonic, sleep=time.sleep,
              popen=subprocess.Popen, killpg=os.killpg if hasattr(os, 'killpg') else None,
              extinct_file=None):
    """Run one owned session, kill its entire group on expiry, confirm extinction.

    The same function is shipped to the remote supervisor and exercised locally.
    Child output goes to a bounded caller-owned file, never an unbounded pipe.
    """
    import tempfile
    def valid():
        try:
            expiry = float(Path(permit).read_text())
            return math.isfinite(expiry) and clock() < expiry
        except (OSError, ValueError):
            return False
    if not valid():
        raise RuntimeError('permit-expired')
    with tempfile.TemporaryFile() as output:
        child = popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                      start_new_session=True)
        writer_error = []
        def feed():
            try:
                child.stdin.write(input_bytes or b'')
                child.stdin.close()
            except (OSError, ValueError) as error:
                writer_error.append(type(error).__name__)
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        cancelled = False
        try:
            while child.poll() is None:
                if not valid() or output.tell() > 24 * 1024 * 1024:
                    cancelled = True
                    break
                sleep(.1)
        finally:
            # Kill descendants even when the group leader has already exited.
            try:
                killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            until = clock() + 1
            while child.poll() is None and clock() < until:
                sleep(.05)
            try:
                killpg(child.pid, getattr(signal, 'SIGKILL', 9))
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            writer.join(timeout=1)
        # No success while a descendant (including a lingering process group) exists.
        until = clock() + 2
        while True:
            try:
                killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if clock() >= until:
                raise RuntimeError('extinction-unconfirmed')
            sleep(.05)
        if extinct_file is not None:
            Path(extinct_file).write_text('yes')
        if cancelled or writer_error or not valid() or child.returncode:
            raise RuntimeError('native-supervised-failure')
        output.seek(0)
        result = output.read(24 * 1024 * 1024 + 1)
        if len(result) > 24 * 1024 * 1024:
            raise RuntimeError('native-output-bounds')
        return result


# All input is JSON on stdin. Commands, paths and project/scheme are fixed here.
REMOTE_COMMON = r'''
import base64, fcntl, hashlib, io, json, math, os, pathlib, plistlib, re, shutil
import signal, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path
def checked(argv, timeout=30, cwd=None):
    capture = (argv[0].endswith('leaf-storage') or
               (argv[:2]==['/usr/bin/xcrun','simctl'] and argv[2] in ('list','listapps')))
    if not capture:
        r=subprocess.run(argv,cwd=cwd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
        if r.returncode: raise RuntimeError('fixed-command-failed')
        return b''
    with tempfile.TemporaryFile() as log:
        r = subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.DEVNULL, timeout=timeout)
        if r.returncode: raise RuntimeError('fixed-command-failed')
        log.seek(0)
        result=log.read(2*1024*1024+1)
        assert len(result)<=2*1024*1024
        return result
def roots(p):
    ticket=os.environ['MINI_ALLOC_TICKET']
    assert ticket == p['ticket'] and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',ticket)
    assert re.fullmatch(r'preview-[0-9a-f-]{36}-[0-9]+-[0-9a-f]{32}',p['name'])
    udids=[value.lower() for value in os.environ['MINI_ALLOC_UDIDS'].split(',')]
    assert udids == [p['udid']]
    storage=str(Path.home()/'bin/leaf-storage')
    def root(kind):
        value=Path(checked([storage,'path','--pinned',ticket,kind]).decode().strip()).resolve()
        assert value.is_absolute()
        return value
    work,cache=root('work'),root('cache')
    source=work/p['name']
    derived=cache/p['name']
    internal=Path('/private/tmp')/p['name']
    assert source.parent == work and derived.parent == cache
    return source,derived,internal
def permitted(source):
    expiry=float((source/'permit').read_text())
    assert math.isfinite(expiry) and time.monotonic() < expiry
'''

BOOTSTRAP = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
source.mkdir(mode=0o700,exist_ok=False)
(source/'owner').write_text(p['name'])
print(json.dumps({'remote_clock':time.monotonic()}))
'''

REFRESH = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
assert source.is_dir() and not source.is_symlink()
expiry=p['expiry']
assert type(expiry) in (int,float) and math.isfinite(expiry)
assert time.monotonic() < expiry <= time.monotonic()+10
temporary=source/'permit.new'
temporary.write_text(str(expiry))
os.chmod(temporary,0o600)
os.replace(temporary,source/'permit')
print('{}')
'''

DRIVER = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
def effect(argv, timeout=120, cwd=None):
    permitted(source)
    result=checked(argv,timeout,cwd)
    permitted(source)
    return result
permitted(source)
raw=base64.b64decode(p['archive'],validate=True)
assert len(raw)==p['byte_size'] and hashlib.sha256(raw).hexdigest()==p['sha256']
project=source/'source'
project.mkdir(mode=0o700)
with zipfile.ZipFile(io.BytesIO(raw)) as z:
    # Windows has already regenerated exactly; repeat extraction fences here.
    infos=z.infolist()
    assert len(infos)<=250 and sum(i.file_size for i in infos)<=80*1024*1024
    assert len(set(i.filename for i in infos))==len(infos)
    for i in infos:
        path=(project/i.filename).resolve()
        assert path.is_relative_to(project) and not i.filename.startswith('/')
        assert ((i.external_attr>>16)&0o170000)==0o100000
    permitted(source)
    z.extractall(project)
permitted(source)
derived.mkdir(mode=0o700,exist_ok=False)
(derived/'owner').write_text(p['name'])
effect(['/usr/bin/xcodebuild','-project','Portfolio.xcodeproj','-scheme','Portfolio',
        '-sdk','iphonesimulator','-destination','id='+p['udid'],'-configuration','Debug',
        '-derivedDataPath',str(derived),'-disableAutomaticPackageResolution',
        '-onlyUsePackageVersionsFromResolvedFile','CODE_SIGNING_ALLOWED=NO','build'],
       max(1,min(1200,p['budget'])),project)
app=derived/'Build/Products/Debug-iphonesimulator/Portfolio.app'
bundle=plistlib.loads((app/'Info.plist').read_bytes())['CFBundleIdentifier']
assert bundle=='com.example.portfolio'
permitted(source)
internal.mkdir(mode=0o700,exist_ok=False)
(internal/'owner').write_text(p['name'])
copy=internal/'Portfolio.app'
effect(['/usr/bin/ditto',str(app),str(copy)],60)
def manifest(folder):
    result={}
    for path in folder.rglob('*'):
        assert not path.is_symlink()
        if path.is_file(): result[str(path.relative_to(folder))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
assert manifest(app)==manifest(copy)
assert plistlib.loads((copy/'Info.plist').read_bytes())['CFBundleIdentifier']==bundle
udid=p['udid']
def sim(*args): return effect(['/usr/bin/xcrun','simctl',*args])
devices=json.loads(sim('list','devices','--json'))['devices']
matches=[(runtime,row) for runtime,rows in devices.items() for row in rows if row['udid'].lower()==udid]
assert len(matches)==1 and matches[0][1].get('isAvailable') is True
runtime=matches[0][0]
assert matches[0][1]['deviceTypeIdentifier']=='com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro-Max'
sim('bootstatus',udid,'-b')
assert bundle not in plistlib.loads(sim('listapps',udid))
# Record custody before install, including an interrupted/uncertain install.
(source/'install-owned').write_text(bundle)
sim('install',udid,str(copy))
sim('launch',udid,bundle)
permitted(source)
time.sleep(3)
shot=internal/'screenshot.png'
sim('io',udid,'screenshot','--type=png',str(shot))
assert 33<=shot.stat().st_size<=16*1024*1024
raw=shot.read_bytes()
permitted(source)
print(json.dumps({'simulator_udid':udid,'simulator_runtime':runtime,'bundle_id':bundle,
    'allocator_ticket':p['ticket'],'build_exit_code':0,'install_exit_code':0,'launch_exit_code':0,
    'screenshot_sha256':hashlib.sha256(raw).hexdigest(),'screenshot_base64':base64.b64encode(raw).decode()}))
'''

SUPERVISOR_END = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
with (source/'supervisor.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    permitted(source)
    driver=p.pop('driver')
    (source/'started').write_text('yes')
    result=supervise(['/usr/bin/python3','-c',driver],source/'permit',
                     input_bytes=json.dumps(p).encode(),extinct_file=source/'extinct')
    sys.stdout.buffer.write(result)
'''

CLEANUP = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
# Cleanup is allowed after cancellation, but never broadens its ownership.
if source.exists():
    assert not source.is_symlink() and (source/'owner').read_text()==p['name']
    (source/'permit').write_text('0')
    lock=(source/'supervisor.lock').open('a')
    until=time.monotonic()+20
    while True:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            break
        except BlockingIOError:
            assert time.monotonic()<until
            time.sleep(.1)
    assert not (source/'started').exists() or (source/'extinct').read_text()=='yes'
def sim(*args):
    return subprocess.run(['/usr/bin/xcrun','simctl',*args],capture_output=True,timeout=30)
udid=p['udid']; bundle='com.example.portfolio'
devices=json.loads(checked(['/usr/bin/xcrun','simctl','list','devices','--json']))['devices']
rows=[row for items in devices.values() for row in items if row['udid'].lower()==udid]
assert len(rows)==1
if rows[0]['state']=='Booted':
    if (source/'install-owned').exists():
        assert (source/'install-owned').read_text()==bundle
        # Terminate may report not running; uninstall and readback establish absence.
        sim('terminate',udid,bundle)
        apps=plistlib.loads(checked(['/usr/bin/xcrun','simctl','listapps',udid]))
        if bundle in apps: assert sim('uninstall',udid,bundle).returncode==0
        apps=plistlib.loads(checked(['/usr/bin/xcrun','simctl','listapps',udid]))
        assert bundle not in apps
    assert sim('shutdown',udid).returncode==0
devices=json.loads(checked(['/usr/bin/xcrun','simctl','list','devices','--json']))['devices']
assert [r['state'] for rows in devices.values() for r in rows if r['udid'].lower()==udid]==['Shutdown']
for path in (internal,derived,source):
    assert not path.is_symlink()
    if path.exists():
        assert (path/'owner').read_text()==p['name']
        shutil.rmtree(path)
    assert not path.exists()
print(json.dumps({'cleanup_complete':True}))
'''


def canonical_allocator():
    require(sys.platform == 'win32', 'windows-worker-required')
    # The installed helper imports its own sibling modules; never resolve a repo copy.
    path = CLIENT_DIR / 'mini_alloc_client.py'
    sys.path.insert(0, str(CLIENT_DIR))
    spec = importlib.util.spec_from_file_location('_canonical_native_allocator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacAdapter:
    def __init__(self, allocator=None):
        self.allocator = allocator
        self.report = {'cleanup_complete': False, 'allocator_release_confirmed': False}

    def __call__(self, archive, job, guard):
        guard.stage('mac-bootstrap')
        guard.check()
        allocator = self.allocator or canonical_allocator()
        # Drop canonical helper diagnostics: no allocator token, app text or raw SSH error escapes.
        client = allocator.Client(host='mac-mini', sleep=guard.sleep, stderr=QuietDiagnostics())
        name = 'preview-' + job['job_id'] + '-' + str(job['attempt']) + '-' + secrets.token_hex(16)
        released = False
        cleanup = False
        facts = image = None
        with allocator.lease('studio-native-preview', name, slots='sim,build', kind='ios-preview',
                             sim_types=('iPhone 17 Pro Max',), markers=(name,), client=client,
                             deadline_s=max(1, guard.end - guard.monotonic()),
                             queue_wait_s=max(1, guard.end - guard.monotonic())) as held:
            release = held.release
            def record_release(*args, **kwargs):
                nonlocal released
                result = release(*args, **kwargs)
                released = getattr(result, 'returncode', None) == 0
                self.report['allocator_release_confirmed'] = released
                return result
            held.release = record_release
            guard.held = held
            guard.check()
            udids = held.env.get('MINI_ALLOC_UDIDS', '').split(',')
            require(len(udids) == 1, 'invalid-granted-simulator')
            udid = canonical_uuid(udids[0].lower())
            p = {'ticket': held.ticket, 'udid': udid, 'name': name}
            require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', held.ticket), 'invalid-ticket')

            def call(code, payload, *, limit=16384, cleanup_call=False, timeout=10):
                if not cleanup_call:
                    guard.check()
                result = held.ssh('/usr/bin/python3 -c ' + shlex.quote(REMOTE_COMMON + code),
                                  payload=json.dumps(payload), timeout=timeout)
                if not cleanup_call:
                    guard.check()
                require(result.returncode == 0 and len(result.stdout.encode()) <= limit, 'mac-transport-failed')
                return parse(result.stdout)

            try:
                bootstrap = call(BOOTSTRAP, p)
                require(set(bootstrap) == {'remote_clock'} and type(bootstrap['remote_clock']) in (int, float)
                        and math.isfinite(bootstrap['remote_clock']), 'invalid-remote-clock')
                # Lower-bound remote monotonic offset: receive delay only shortens validity.
                offset = bootstrap['remote_clock'] - guard.monotonic()
                def refresh(expiry):
                    guard.check()
                    call(REFRESH, {**p, 'expiry': expiry + offset})
                with guard.lock:
                    guard.refresh = refresh
                    guard.stage('mac-permit-refresh')
                    guard.beat()
                guard.stage('mac-producer')
                payload = {**p, 'archive': base64.b64encode(archive).decode(),
                           'sha256': job['descriptor']['archive_sha256'], 'byte_size': len(archive),
                           'budget': max(1, guard.end - guard.monotonic()),
                           'driver': REMOTE_COMMON + DRIVER}
                code = inspect.getsource(supervise) + SUPERVISOR_END
                facts = call(code, payload, limit=MAX_TRANSFER,
                             timeout=max(1, min(1300, guard.end - guard.monotonic())))
                guard.stage('evidence')
                encoded = facts.pop('screenshot_base64')
                require(type(encoded) is str and len(encoded) <= MAX_TRANSFER, 'screenshot-bounds')
                image = validate_png(base64.b64decode(encoded, validate=True))
                require(hashlib.sha256(image).hexdigest() == facts['screenshot_sha256'], 'screenshot-integrity')
                guard.check()
            finally:
                # Synchronize with the refresher before deleting its owned permit directory.
                with guard.lock:
                    guard.refresh = None
                try:
                    cleanup = call(CLEANUP, p, cleanup_call=True, timeout=180) == {'cleanup_complete': True}
                except BaseException:
                    cleanup = False
                self.report['cleanup_complete'] = cleanup
                held.outcome = 'ok' if cleanup and facts else 'infra'
        guard.held = None
        require(cleanup and released and facts is not None and image is not None, 'cleanup-release-unconfirmed')
        guard.check()
        facts.update(cleanup_complete=cleanup, allocator_release_confirmed=released)
        return facts, image
