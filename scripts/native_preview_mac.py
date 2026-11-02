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
import queue
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time

import native_preview_worker as source_policy
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
        permit_expired = False
        try:
            while child.poll() is None:
                permit_expired = not valid()
                if permit_expired or output.tell() > 24 * 1024 * 1024:
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
        permit_expired = permit_expired or not valid()
        if cancelled or writer_error or permit_expired or child.returncode:
            error = RuntimeError('native-supervised-failure')
            error.permit_expired = permit_expired
            raise error
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
def permit_clock():
    # Python 3.9 on macOS gives time.monotonic() a process-local origin.
    # Every permit reader and writer must use the shared kernel clock.
    return time.clock_gettime(time.CLOCK_MONOTONIC)

def checked(argv, timeout=30, cwd=None):
    capture = (argv in (['/usr/bin/xcodebuild','-version'], ['/opt/homebrew/bin/xcodegen','--version'],
                         ['/usr/bin/xcrun','--sdk','iphonesimulator','--show-sdk-version']) or
               argv[0].endswith('leaf-storage') or
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
def application_inventory(raw):
    # simctl emits an OpenStep plist; plistlib accepts only XML or binary.
    assert type(raw) is bytes and len(raw)<=2*1024*1024
    with tempfile.TemporaryFile() as log:
        result=subprocess.run(['/usr/bin/plutil','-convert','xml1','-o','-','--','-'],
                              input=raw,stdout=log,stderr=subprocess.DEVNULL,timeout=30)
        assert result.returncode==0
        log.seek(0)
        converted=log.read(2*1024*1024+1)
        assert len(converted)<=2*1024*1024
    value=plistlib.loads(converted)
    assert type(value) is dict
    return value

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
    assert math.isfinite(expiry) and permit_clock() < expiry
'''

BOOTSTRAP = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
source.mkdir(mode=0o700,exist_ok=False)
(source/'owner').write_text(p['name'])
print(json.dumps({'remote_clock':permit_clock()}))
'''

REFRESH = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
assert source.is_dir() and not source.is_symlink()
expiry=p['expiry']
assert type(expiry) in (int,float) and math.isfinite(expiry)
assert permit_clock() < expiry <= permit_clock()+10
temporary=source/'permit.new'
temporary.write_text(str(expiry))
os.chmod(temporary,0o600)
os.replace(temporary,source/'permit')
print('{}')
'''

PERMIT_STREAM = r'''
def closed_line(limit):
    raw=sys.stdin.buffer.readline(limit+1)
    if not raw: return None
    assert len(raw)<=limit and raw.endswith(b'\n')
    def pairs(items):
        value={}
        for key,item in items:
            assert key not in value
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs)

p=closed_line(16384)
assert type(p) is dict and set(p)=={'ticket','udid','name'}
source,derived,internal=roots(p)
def owned():
    assert source.is_dir() and not source.is_symlink()
    owner=source/'owner'
    assert not owner.is_symlink() and owner.is_file() and owner.stat().st_size<=256
    assert owner.read_text()==p['name']
def write_permit(expiry):
    owned()
    assert not (source/'permit').is_symlink()
    fd,name=tempfile.mkstemp(prefix='.permit-',dir=source)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(str(expiry))
        os.replace(name,source/'permit')
    finally:
        if os.path.exists(name): os.unlink(name)
owned()
fd=os.open(source/'stream.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
with os.fdopen(fd,'a+') as lock:
    import stat
    assert stat.S_ISREG(os.fstat(lock.fileno()).st_mode)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        print('{"ready":true}',flush=True)
        while True:
            value=closed_line(1024)
            if value is None: break
            assert type(value) is dict and set(value)=={'expiry'}
            expiry=value['expiry']
            assert type(expiry) in (int,float) and math.isfinite(expiry)
            assert permit_clock()<expiry<=permit_clock()+10
            write_permit(expiry)
            print('{}',flush=True)
    finally:
        # Only this lock owner may revoke; never touch a replaced/foreign tree.
        write_permit(0)
'''


class PermitStream:
    """One bounded permit channel inside the current canonical allocator lease."""
    def __init__(self, allocator, held, context, guard, offset):
        self.held, self.guard, self.offset = held, guard, offset
        self.clock, self.end = guard.monotonic, guard.end
        self.lock = threading.Lock()
        self.replies = queue.Queue(maxsize=2)
        self.failed = threading.Event()
        self.child = self.reader = self.writer = None
        self.closed = False
        try:
            guard.check()
            require(not held.lost and self.end > self.clock(), 'mac-transport-failed')
            args = ['run', '--ticket', held.ticket, '--timeout-s',
                    str(max(1, math.ceil(self.end-self.clock()))), '--',
                    '/usr/bin/python3', '-u', '-c', REMOTE_COMMON + PERMIT_STREAM]
            self.child = held.client.popen(allocator.ssh_argv(args, host=held.client.host),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            until = min(self.end, self.clock()+10)
            self._send(context, 16384, until)
            self._ack({'ready': True}, until)
        except BaseException:
            self.close()
            raise WorkerError('mac-transport-failed') from None

    def _read(self):
        try:
            while True:
                line = self.child.stdout.readline(1025)
                if not line or len(line)>1024 or not line.endswith(b'\n'):
                    self.failed.set()
                    return
                self.replies.put_nowait(line)
        except BaseException:
            self.failed.set()

    def _remaining(self, until):
        remaining = min(until, self.end)-self.clock()
        require(remaining > 0 and not self.failed.is_set(), 'mac-transport-failed')
        return remaining

    def _send(self, value, limit, until):
        raw = json.dumps(value,allow_nan=False,separators=(',',':')).encode()+b'\n'
        require(len(raw)<=limit, 'mac-transport-failed')
        errors = []
        def write():
            try:
                remaining = memoryview(raw)
                while remaining:
                    count = self.child.stdin.write(remaining)
                    if not count: raise OSError()
                    remaining = remaining[count:]
                self.child.stdin.flush()
            except BaseException:
                errors.append(True)
        self.writer = threading.Thread(target=write,daemon=True)
        self.writer.start()
        self.writer.join(timeout=self._remaining(until))
        require(not self.writer.is_alive() and not errors, 'mac-transport-failed')

    def _ack(self, expected, until):
        try:
            line = self.replies.get(timeout=self._remaining(until))
            reply = parse(line)
            require(reply==expected and type(reply) is dict
                    and all(type(reply[key]) is type(value) for key,value in expected.items())
                    and not self.failed.is_set(), 'mac-transport-failed')
        except BaseException:
            raise WorkerError('mac-transport-failed') from None

    def grant(self, expiry):
        with self.lock:
            try:
                self.guard.check()
                require(not self.closed and not self.held.lost and self.child.poll() is None
                        and self.replies.empty(), 'mac-transport-failed')
                until = min(self.end,self.clock()+4)
                self._send({'expiry': expiry+self.offset},1024,until)
                self._ack({},until)
                self.guard.check()
            except BaseException:
                self.failed.set()
                raise WorkerError('mac-transport-failed') from None

    def close(self):
        self.closed = True
        child = self.child
        if child is None: return True
        try:
            # A blocked pipe writer must be released by killing its local SSH.
            if self.writer is not None and self.writer.is_alive():
                child.kill()
            child.stdin.close()
            try: child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
            if self.reader is not None: self.reader.join(timeout=1)
            if self.writer is not None: self.writer.join(timeout=1)
            return (child.poll() is not None
                    and (self.reader is None or not self.reader.is_alive())
                    and (self.writer is None or not self.writer.is_alive()))
        except BaseException:
            try:
                child.kill()
                child.wait(timeout=2)
            except BaseException: pass
            return False
        finally:
            try: child.stdout.close()
            except BaseException: pass

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
native = p.get('descriptor',{}).get('schema') == 'leaf.project-repository-preview-source.v1'
if native: verify_native_archive(raw,p['descriptor'])
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
project_name='LeafProject' if native else 'Portfolio'
expected_bundle=('private.leaf.project.p'+p['descriptor']['project_id'].replace('-','')) if native else 'com.example.portfolio'
if native:
    effect(['/opt/homebrew/bin/xcodegen','generate','--spec','project.yml'],60,project)
effect(['/usr/bin/xcodebuild','-project',project_name+'.xcodeproj','-scheme',project_name,
        '-sdk','iphonesimulator','-destination','generic/platform=iOS Simulator','-configuration','Debug',
        '-derivedDataPath',str(derived),'-disableAutomaticPackageResolution',
        '-onlyUsePackageVersionsFromResolvedFile','CODE_SIGNING_ALLOWED=NO','build'],
       max(1,min(1200,p['budget'])),project)
app=derived/('Build/Products/Debug-iphonesimulator/'+project_name+'.app')
bundle=plistlib.loads((app/'Info.plist').read_bytes())['CFBundleIdentifier']
assert bundle==expected_bundle
permitted(source)
internal.mkdir(mode=0o700,exist_ok=False)
(internal/'owner').write_text(p['name'])
copy=internal/(project_name+'.app')
effect(['/usr/bin/ditto',str(app),str(copy)],60)
def manifest(folder):
    result={}
    for path in folder.rglob('*'):
        assert not path.is_symlink()
        if path.is_file(): result[str(path.relative_to(folder))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
app_manifest=manifest(app)
assert app_manifest==manifest(copy)
assert plistlib.loads((copy/'Info.plist').read_bytes())['CFBundleIdentifier']==bundle
if native:
    def tool(argv):
        raw=effect(argv,30)
        assert type(raw) is bytes and 0<len(raw)<=256
        return raw.decode('ascii').strip()
    build_artifact={'source':native_identity(p['descriptor']),'bundle_id':bundle,
        'sdk':'iphonesimulator','app_tree_sha256':hashlib.sha256(_native_json(app_manifest)).hexdigest(),
        'file_count':len(app_manifest),'xcodegen_exit_code':0,
        'xcode_version':tool(['/usr/bin/xcodebuild','-version']),
        'xcodegen_version':tool(['/opt/homebrew/bin/xcodegen','--version']),
        'sdk_version':tool(['/usr/bin/xcrun','--sdk','iphonesimulator','--show-sdk-version'])}
    (source/'sdk-build.json').write_text(json.dumps(build_artifact,sort_keys=True,separators=(',',':')))
udid=p['udid']
def sim(*args): return effect(['/usr/bin/xcrun','simctl',*args])
devices=json.loads(sim('list','devices','--json'))['devices']
matches=[(runtime,row) for runtime,rows in devices.items() for row in rows if row['udid'].lower()==udid]
assert len(matches)==1 and matches[0][1].get('isAvailable') is True
runtime=matches[0][0]
assert matches[0][1]['deviceTypeIdentifier']=='com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro-Max'
sim('bootstatus',udid,'-b')
assert bundle not in application_inventory(sim('listapps',udid))
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
result={'simulator_udid':udid,'simulator_runtime':runtime,'bundle_id':bundle,
    'allocator_ticket':p['ticket'],'build_exit_code':0,'install_exit_code':0,'launch_exit_code':0,
    'screenshot_sha256':hashlib.sha256(raw).hexdigest(),'screenshot_base64':base64.b64encode(raw).decode()}
if native: result['build_artifact']=build_artifact
print(json.dumps(result))
'''

SUPERVISOR_END = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
with (source/'supervisor.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    permitted(source)
    driver=p.pop('driver')
    (source/'started').write_text('yes')
    try:
        result=supervise(['/usr/bin/python3','-c',driver],source/'permit',
                         input_bytes=json.dumps(p).encode(),clock=permit_clock,
                         extinct_file=source/'extinct')
    except RuntimeError as error:
        if getattr(error,'permit_expired',False) or str(error)=='permit-expired': sys.exit(71)
        if str(error)=='native-supervised-failure': sys.exit(72)
        if str(error)=='extinction-unconfirmed': sys.exit(73)
        raise
    sys.stdout.buffer.write(result)
'''

SDK_BUILD_READ = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
assert source.is_dir() and not source.is_symlink() and (source/'owner').read_text()==p['name']
path=source/'sdk-build.json'
if not path.exists(): print('{"build_artifact":null}')
else:
    assert not path.is_symlink() and path.is_file() and path.stat().st_size<=8192
    print(json.dumps({'build_artifact':json.loads(path.read_bytes())}))
'''

CLEANUP = r'''
p=json.load(sys.stdin)
source,derived,internal=roots(p)
# Cleanup is allowed after cancellation, but never broadens its ownership.
if source.exists():
    assert not source.is_symlink() and (source/'owner').read_text()==p['name']
    # EOF revocation must finish before cleanup can delete the owned source.
    stream_fd=os.open(source/'stream.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    stream_lock=os.fdopen(stream_fd,'a+')
    import stat
    assert stat.S_ISREG(os.fstat(stream_lock.fileno()).st_mode)
    until=time.monotonic()+20
    while True:
        try:
            fcntl.flock(stream_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            break
        except BlockingIOError:
            assert time.monotonic()<until
            time.sleep(.1)
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
if 'native_project_id' in p:
    assert type(p['native_project_id']) is str and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',p['native_project_id'])
    bundle='private.leaf.project.p'+p['native_project_id'].replace('-','')
devices=json.loads(checked(['/usr/bin/xcrun','simctl','list','devices','--json']))['devices']
rows=[row for items in devices.values() for row in items if row['udid'].lower()==udid]
assert len(rows)==1
if rows[0]['state']=='Booted':
    if (source/'install-owned').exists():
        assert (source/'install-owned').read_text()==bundle
        # Terminate may report not running; uninstall and readback establish absence.
        sim('terminate',udid,bundle)
        apps=application_inventory(checked(['/usr/bin/xcrun','simctl','listapps',udid]))
        if bundle in apps: assert sim('uninstall',udid,bundle).returncode==0
        apps=application_inventory(checked(['/usr/bin/xcrun','simctl','listapps',udid]))
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


def reconciliation_transport_bounded(argv, *, input, timeout):
    # Bound storage as well as reads; no stderr or raw transport errors escape.
    import tempfile
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output,
                                 stderr=subprocess.DEVNULL)
        try:
            child.stdin.write(input)
            child.stdin.close()
            until = time.monotonic() + timeout
            while child.poll() is None:
                require(os.fstat(output.fileno()).st_size <= 16384, 'response-bounds')
                require(time.monotonic() < until, 'mac-transport-failed')
                time.sleep(.02)
            output.seek(0)
            raw = output.read(16385)
            require(len(raw) <= 16384, 'response-bounds')
            return subprocess.CompletedProcess(argv, child.returncode, raw)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


RECONCILIATION_READ = r'''
import hashlib, json, math, re, sys, uuid
from pathlib import Path
def pairs(items):
    out={}
    for key,value in items:
        assert key not in out
        out[key]=value
    return out
def load(raw):
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:bad())
def bad():
    raise ValueError()
def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def read(path,limit):
    assert not path.is_symlink() and path.is_file()
    with path.open('rb') as stream: raw=stream.read(limit+1)
    assert len(raw)<=limit
    return raw
def strings(value):
    if isinstance(value,str): yield value
    elif isinstance(value,dict):
        for key,item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value,list):
        for item in value: yield from strings(item)
def main():
    p=load(sys.stdin.buffer.read(1025))
    assert set(p)=={'job_id','attempt'}
    jid,attempt=p['job_id'],p['attempt']
    assert type(jid) is str and str(uuid.UUID(jid))==jid
    assert type(attempt) is int and 1<=attempt<=2147483647
    prefix='preview-'+jid+'-'+str(attempt)+'-'
    root=Path.home()/'.mini-alloc'
    assert root.is_dir() and not root.is_symlink()
    before=load(read(root/'state.json',8*1024*1024))
    assert type(before) is dict
    paths=[root/('history.jsonl'+suffix) for suffix in ('.5','.4','.3','.2','.1','')]
    assert all(not p.is_symlink() for p in paths)
    # Rotation may retain fewer than five files, but a hole cannot certify a
    # complete window: it could hide conflicting evidence for this ticket.
    present=[p.exists() for p in reversed(paths)]
    assert present[0] and present==sorted(present,reverse=True)
    signatures=[(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else None for p in paths]
    events=[]; total=0
    for path in paths:
        if not path.exists():
            assert path.name!='history.jsonl'
            continue
        raw=read(path,11*1024*1024); total+=len(raw)
        assert total<=66*1024*1024 and (not raw or raw.endswith(b'\n'))
        for line in raw.splitlines():
            row=load(line)
            assert type(row) is dict
            events.append(row)
    assert signatures==[(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else None for p in paths]
    after=load(read(root/'state.json',8*1024*1024))
    assert type(after) is dict
    associated=[]; markers=set()
    for row in events:
        matching=[v for v in strings(row) if v.startswith(prefix)]
        if row.get('event') in ('submit','grant') and matching:
            assert all(re.fullmatch(re.escape(prefix)+r'(?:[0-9a-f]{32}|<redacted>)',v) for v in matching)
            ticket=row.get('ticket')
            assert type(ticket) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',ticket)
            associated.append(ticket)
            markers.update(matching)
    tickets=set(associated)
    assert len(tickets)==1 and len(markers)==1
    ticket=tickets.pop()
    # A full ticket must not also describe another preview execution. Joining
    # only its terminal events would otherwise hide a conflicting association.
    for row in events:
        matching=[v for v in strings(row) if v.startswith(prefix)]
        if matching:
            # History.append redacts random suffixes and records census sightings
            # without a ticket. A historical sighting is not a ticket association.
            if row.get('event')=='legacy_seen':
                assert set(row)=={'event','marker','ts','redactions'}
                assert row['marker'] in markers
                assert type(row['ts']) in (int,float) and math.isfinite(row['ts'])
                assert type(row['redactions']) is int and row['redactions']>=0
                continue
            assert row.get('ticket')==ticket and all(v in markers for v in matching)
        if row.get('ticket')!=ticket: continue
        names=[v for v in strings(row) if v.startswith('preview-')]
        assert all(v in markers for v in names)
    def active_matches(state):
        # The allocator retains terminal rows for free_retention_s. They are
        # evidence of release, not active admission, only after confirmed death.
        assert type(state.get('jobs')) is dict
        count=sum(v==ticket or v.startswith(prefix)
                  for v in strings({k:v for k,v in state.items() if k!='jobs'}))
        for key,row in state['jobs'].items():
            if not (key==ticket or any(v==ticket or v.startswith(prefix) for v in strings(row))):
                continue
            assert type(row) is dict
            if (key==ticket and row.get('ticket',key)==ticket
                    and row.get('state')=='free' and row.get('confirmed_dead') is True):
                continue
            count+=1
        return count
    counts=[active_matches(state) for state in (before,after)]
    assert counts==[0,0]
    times={}
    ordered=[]
    for row in events:
        if row.get('ticket')!=ticket: continue
        event=row.get('event')
        if event not in ('grant','release','confirm_dead','free'): continue
        keys=[key for key in ('ts','at','time') if key in row]
        # Canonical History.append stamps ts. The free producer also retains
        # its identical at field; divergent clocks remain ambiguous.
        assert keys==['ts'] or (keys==['ts','at'] and event=='free' and row['ts']==row['at'])
        stamp=row['ts']
        assert type(stamp) in (int,float) and math.isfinite(stamp)
        assert event not in times
        times[event]=stamp; ordered.append(event)
    assert ordered==['grant','release','confirm_dead','free']
    assert times['grant']<=times['release']<=times['confirm_dead']<=times['free']
    value=dict(job_id=jid,attempt=attempt,prefix=prefix,ticket=ticket,event_times=times,
               active_match_counts=counts,history_complete=True)
    value['witness_sha256']=hashlib.sha256(canonical(value)).hexdigest()
    sys.stdout.buffer.write(canonical(value))
try: main()
except OSError: sys.exit(75)
except BaseException: sys.exit(1)
'''


class MacAdapter:
    def __init__(self, allocator=None, *, reconciliation_transport=None, permit_factory=PermitStream):
        self.allocator = allocator
        self.reconciliation_transport = reconciliation_transport or reconciliation_transport_bounded
        self.permit_factory = permit_factory
        self.report = {'cleanup_complete': False, 'allocator_release_confirmed': False}

    def read_reconciliation_allocator(self, job_id, attempt):
        from native_preview_reconciliation import allocator_witness
        canonical_uuid(job_id)
        require(type(attempt) is int and 1 <= attempt <= 2147483647)
        allocator = self.allocator or canonical_allocator()
        argv = allocator.ssh_argv([], host='mac-mini')
        require(type(argv) is list and len(argv) >= 3 and argv[-2] == 'mac-mini')
        argv = argv[:-1] + ['/usr/bin/python3 -c ' + shlex.quote(RECONCILIATION_READ)]
        try:
            result = self.reconciliation_transport(argv, input=json.dumps(
                {'job_id': job_id, 'attempt': attempt}).encode(), timeout=30)
        except BaseException:
            raise WorkerError('mac-transport-failed') from None
        require(result.returncode not in (75, 255), 'mac-transport-failed')
        require(result.returncode == 0 and type(result.stdout) is bytes
                and len(result.stdout) <= 16384)
        return allocator_witness(parse(result.stdout), job_id, attempt)

    def __call__(self, archive, job, guard):
        native_source = job.get('descriptor', {}).get('schema') == source_policy.NATIVE_SCHEMA
        if native_source:
            source_policy.verify_source(archive, job['descriptor'])
        guard.stage('mac-bootstrap')
        guard.check()
        allocator = self.allocator or canonical_allocator()
        # Drop canonical helper diagnostics: no allocator token, app text or raw SSH error escapes.
        client = allocator.Client(host='mac-mini', sleep=guard.sleep, stderr=QuietDiagnostics())
        name = 'preview-' + job['job_id'] + '-' + str(job['attempt']) + '-' + secrets.token_hex(16)
        released = False
        cleanup = False
        facts = image = None
        stream = None
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
                guard.record({'event': 'release', 'allocator_release_confirmed': released})
                return result
            held.release = record_release
            guard.held = held
            guard.check()
            udids = held.env.get('MINI_ALLOC_UDIDS', '').split(',')
            require(len(udids) == 1, 'invalid-granted-simulator')
            udid = canonical_uuid(udids[0].lower())
            p = {'ticket': held.ticket, 'udid': udid, 'name': name}
            require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', held.ticket), 'invalid-ticket')

            def call(code, payload, *, limit=16384, cleanup_call=False, producer_call=False, timeout=10):
                if not cleanup_call:
                    guard.check()
                result = held.ssh('/usr/bin/python3 -c ' + shlex.quote(REMOTE_COMMON + code),
                                  payload=json.dumps(payload), timeout=timeout)
                if not cleanup_call:
                    guard.check()
                if producer_call and result.returncode in (71,72,73):
                    raise WorkerError({71:'mac-permit-expired',72:'mac-producer-failed',
                                       73:'mac-extinction-unconfirmed'}[result.returncode])
                require(result.returncode == 0 and len(result.stdout.encode()) <= limit, 'mac-transport-failed')
                return parse(result.stdout)

            try:
                guard.record({'event': 'allocator', 'ticket': held.ticket,
                              'marker_sha256': hashlib.sha256(name.encode()).hexdigest()})
                bootstrap = call(BOOTSTRAP, p)
                require(set(bootstrap) == {'remote_clock'} and type(bootstrap['remote_clock']) in (int, float)
                        and math.isfinite(bootstrap['remote_clock']), 'invalid-remote-clock')
                # Lower-bound remote monotonic offset: receive delay only shortens validity.
                offset = bootstrap['remote_clock'] - guard.monotonic()
                stream = self.permit_factory(allocator, held, p, guard, offset)
                with guard.lock:
                    guard.refresh = stream.grant
                    guard.stage('mac-permit-refresh')
                    guard.beat()
                guard.record({'event': 'producer', 'producer_may_have_started': True})
                guard.stage('mac-producer')
                payload = {**p, 'archive': base64.b64encode(archive).decode(),
                           'sha256': job['descriptor']['archive_sha256'], 'byte_size': len(archive),
                           'budget': max(1, guard.end - guard.monotonic()),
                           'driver': REMOTE_COMMON + DRIVER}
                if native_source:
                    payload['descriptor'] = job['descriptor']
                    payload['driver'] = REMOTE_COMMON + native_remote_policy() + DRIVER
                code = inspect.getsource(supervise) + SUPERVISOR_END
                facts = call(code, payload, limit=MAX_TRANSFER, producer_call=True,
                             timeout=max(1, min(1300, guard.end - guard.monotonic())))
                guard.stage('evidence')
                encoded = facts.pop('screenshot_base64')
                require(type(encoded) is str and len(encoded) <= MAX_TRANSFER, 'screenshot-bounds')
                image = validate_png(base64.b64decode(encoded, validate=True))
                require(hashlib.sha256(image).hexdigest() == facts['screenshot_sha256'], 'screenshot-integrity')
                guard.check()
            finally:
                if native_source:
                    try:
                        sdk = call(SDK_BUILD_READ, p, cleanup_call=True, timeout=10)
                        if sdk.get('build_artifact') is not None:
                            source_policy.validate_native_artifact(sdk['build_artifact'], job['descriptor'])
                            self.report['build_artifact'] = sdk['build_artifact']
                    except BaseException:
                        pass  # Missing/uncertain evidence never becomes SDK success.
                # Synchronize with the refresher before deleting its owned permit directory.
                with guard.lock:
                    guard.refresh = None
                stream_closed = stream is None or stream.close()
                try:
                    cleanup_payload = dict(p, native_project_id=job['descriptor']['project_id']) if native_source else p
                    cleanup = (call(CLEANUP, cleanup_payload, cleanup_call=True, timeout=180) == {'cleanup_complete': True}
                               and stream_closed)
                except BaseException:
                    cleanup = False
                self.report['cleanup_complete'] = cleanup
                held.outcome = 'ok' if cleanup and facts else 'infra'
                guard.record({'event': 'cleanup', 'cleanup_complete': cleanup})
        guard.held = None
        require(cleanup and released and facts is not None and image is not None, 'cleanup-release-unconfirmed')
        guard.check()
        facts.update(cleanup_complete=cleanup, allocator_release_confirmed=released)
        return facts, image


def native_remote_policy():
    """Ship only the captured pure package validator, never source-owned code."""
    names = ('NATIVE_SCHEMA', 'NATIVE_PROFILE', 'NATIVE_LIMIT', 'NATIVE_MANIFEST',
             'NATIVE_FIELDS', 'NATIVE_APP_SOURCE', 'NATIVE_PROJECT_SPEC')
    constants = '\nimport stat, re\n' + '\n'.join(name+' = '+repr(getattr(source_policy, name)) for name in names)+'\n'
    functions = ('_native_need', '_native_json', 'native_identity', 'native_descriptor',
        '_native_path', 'native_tree', 'native_files_policy', 'native_archive', 'verify_native_archive')
    return constants + '\n'.join(inspect.getsource(getattr(source_policy, name)) for name in functions)+'\n'
