"""Dormant immutable iOS source custody; no build, job, or simulator proof.

Descriptors identify saved source. They never grant access or supply store keys.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
import hashlib
import json
import re
from uuid import UUID

from leaf_platform import project_lifecycle, project_web_page
from leaf_platform.portfolio_ios_template import MAX_ARCHIVE_BYTES


SCHEMA = 'leaf.project-preview-source.v1'
MAX_MANIFEST_BYTES = 2048
_FIELDS = frozenset(('schema', 'target', 'org_id', 'project_id', 'revision',
                     'document_sha256', 'archive_sha256', 'byte_size'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_PRIVATE_ERRORS = (project_lifecycle.LifecycleForbidden,
                   project_lifecycle.LifecycleUnavailable,
                   project_lifecycle.LifecycleConflict)


class SourceStoreUnavailable(RuntimeError):
    """Persisted source could not be published or verified."""


def _uuid(value):
    if isinstance(value, UUID):
        return str(value)
    if type(value) is not str:
        raise ValueError('Invalid source scope.')
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError('Invalid source scope.') from None
    if canonical != value:
        raise ValueError('Invalid source scope.')
    return canonical


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError('Invalid source revision.')
    return value


def _digest(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise ValueError('Invalid source digest.')
    return value


def _descriptor(value, org_id, project_id):
    if type(value) is dict and value.get('schema') == NATIVE_SCHEMA:
        return native_descriptor(value, org_id, project_id)
    if type(value) is not dict or set(value) != _FIELDS:
        raise ValueError('Invalid source descriptor.')
    value = value.copy()
    if (type(value['schema']) is not str or value['schema'] != SCHEMA
            or type(value['target']) is not str or value['target'] != 'ios-simulator'
            or type(value['org_id']) is not str or type(value['project_id']) is not str
            or _uuid(value['org_id']) != org_id or _uuid(value['project_id']) != project_id):
        raise ValueError('Invalid source descriptor scope.')
    _revision(value['revision'])
    _digest(value['document_sha256'])
    _digest(value['archive_sha256'])
    if type(value['byte_size']) is not int or not 0 < value['byte_size'] <= MAX_ARCHIVE_BYTES:
        raise ValueError('Invalid source size.')
    return value


def _keys(descriptor):
    if descriptor.get('schema') == NATIVE_SCHEMA:
        scope = f"tenants/{descriptor['org_id']}/projects/{descriptor['project_id']}/previews/ios/repository-source"
        return (scope+'/'+descriptor['archive_sha256'], scope+'/manifests/'+descriptor['archive_sha256']+'.json')
    scope = (f"tenants/{descriptor['org_id']}/projects/{descriptor['project_id']}"
             '/previews/ios/source')
    return (f"{scope}/{descriptor['archive_sha256']}",
            f"{scope}/manifests/{descriptor['revision']}/{descriptor['document_sha256']}.json")


def _manifest(descriptor):
    return json.dumps(descriptor, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _page_identity(page, revision):
    if type(page) is not dict or type(page.get('revision')) is not int or page['revision'] != revision:
        raise ValueError('Saved source revision does not match.')
    return _digest(page.get('digest'))


def _read_persisted(backend, descriptor):
    archive_key, manifest_key = _keys(descriptor)
    manifest = backend.get(manifest_key)
    # Requiring the exact canonical serialization also rejects duplicate JSON
    # fields, unknown fields, and bool/int equality tricks without parsing them.
    if (type(manifest) is not bytes or not 0 < len(manifest) <= (16384 if descriptor['schema'] == NATIVE_SCHEMA else MAX_MANIFEST_BYTES)
            or manifest != _manifest(descriptor)):
        raise ValueError('Source manifest integrity failure.')
    raw = backend.get(archive_key)
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES
            or len(raw) != descriptor['byte_size']
            or hashlib.sha256(raw).hexdigest() != descriptor['archive_sha256']):
        raise ValueError('Source archive integrity failure.')
    return raw


def prepare_source(org_id, project_id, actor_id, *, revision):
    """Capture the exact authorized saved revision through the existing exporter."""
    org_id, project_id, actor_id = map(_uuid, (org_id, project_id, actor_id))
    revision = _revision(revision)
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    saved = project_web_page.read(org_id, project_id, actor_id, revision=revision)
    digest = _page_identity(saved, revision)
    page, raw = project_web_page.export_source(org_id, project_id, actor_id,
                                              revision=revision, target='ios')
    if _page_identity(page, revision) != digest:
        raise ValueError('Exported source does not match the saved page.')
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        raise ValueError('Invalid source archive size.')
    descriptor = _descriptor({
        'schema': SCHEMA, 'target': 'ios-simulator', 'org_id': org_id,
        'project_id': project_id, 'revision': revision, 'document_sha256': digest,
        'archive_sha256': hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw),
    }, org_id, project_id)
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(org_id)
        archive_key, manifest_key = _keys(descriptor)
        backend.put_if_absent_or_verify(archive_key, raw)
        # Never remove an archive if manifest publication loses a conflict.
        backend.put_if_absent_or_verify(manifest_key, _manifest(descriptor))
        _read_persisted(backend, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    return descriptor


def read_source(org_id, project_id, actor_id, descriptor):
    """Read verified persisted source under current saved-page authority."""
    org_id, project_id, actor_id = map(_uuid, (org_id, project_id, actor_id))
    descriptor = _descriptor(descriptor, org_id, project_id)
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    if descriptor['schema'] == NATIVE_SCHEMA:
        return read_native_source(org_id, project_id, actor_id, descriptor)
    page = project_web_page.read(org_id, project_id, actor_id, revision=descriptor['revision'])
    if _page_identity(page, descriptor['revision']) != descriptor['document_sha256']:
        raise ValueError('Saved source digest does not match.')
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(org_id)
        raw = _read_persisted(backend, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    project_lifecycle.require_project_role(org_id, project_id, actor_id, write=False)
    return raw

# Captured from nativeProjectSourceProfile.ts. Tests bind these bytes to that producer.
NATIVE_SCHEMA = 'leaf.project-repository-preview-source.v1'
NATIVE_PROFILE = 'ios-swiftui-xcodegen-v1'
NATIVE_LIMIT = 16 * 1024 * 1024
NATIVE_MANIFEST = '.leaf-native-sdk-manifest.json'
NATIVE_FIELDS = frozenset(('schema', 'target', 'source_profile', 'org_id', 'project_id',
    'actor_binding_id', 'repo_key', 'source_request_id', 'source_operation', 'source_version',
    'receipt_digest', 'source_commit', 'source_tree', 'material_refs', 'bundle_sha256',
    'archive_sha256', 'byte_size'))
NATIVE_APP_SOURCE = 'import SwiftUI\n\n@main\nstruct LeafProjectApp: App {\n    var body: some Scene {\n        WindowGroup {\n            ContentView()\n        }\n    }\n}\n'
NATIVE_PROJECT_SPEC = 'name: LeafProject\noptions:\n  deploymentTarget:\n    iOS: "17.0"\nsettings:\n  base:\n    SWIFT_VERSION: "5.0"\n    IPHONEOS_DEPLOYMENT_TARGET: "17.0"\n    SDKROOT: iphonesimulator\n    SUPPORTED_PLATFORMS: iphonesimulator\n    SUPPORTS_MACCATALYST: NO\n    CODE_SIGNING_ALLOWED: NO\n    CODE_SIGNING_REQUIRED: NO\ntargets:\n  LeafProject:\n    type: application\n    platform: iOS\n    sources:\n      - path: Sources\n      - path: Resources/Input\n        type: folder\n        buildPhase: resources\n        optional: true\n    settings:\n      base:\n        PRODUCT_BUNDLE_IDENTIFIER: ${bundleId}\n        PRODUCT_NAME: LeafProject\n        GENERATE_INFOPLIST_FILE: YES\n        INFOPLIST_KEY_UILaunchScreen_Generation: YES\n        TARGETED_DEVICE_FAMILY: "1,2"\n        MARKETING_VERSION: "1.0"\n        CURRENT_PROJECT_VERSION: "1"\n'


def _native_need(value):
    if not value:
        raise ValueError('Invalid Native source custody.')


def _native_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def native_identity(descriptor):
    if descriptor.get('schema') == NATIVE_SCHEMA:
        return {k: descriptor[k] for k in ('schema', 'source_profile', 'repo_key',
            'source_request_id', 'source_version', 'receipt_digest', 'source_commit',
            'source_tree', 'archive_sha256')}
    return {k: descriptor[k] for k in ('revision', 'document_sha256', 'archive_sha256')}


def native_descriptor(value, org, project):
    from uuid import UUID
    _native_need(type(value) is dict and set(value) == NATIVE_FIELDS)
    d = dict(value)
    _native_need(d['schema'] == NATIVE_SCHEMA and d['target'] == 'ios-simulator'
        and d['source_profile'] == NATIVE_PROFILE and d['source_operation'] in ('create', 'edit')
        and d['org_id'] == org and d['project_id'] == project)
    for key in ('org_id', 'project_id', 'actor_binding_id', 'repo_key', 'source_request_id'):
        _native_need(type(d[key]) is str and str(UUID(d[key])) == d[key] and UUID(d[key]).int != 0)
    for key in ('receipt_digest', 'bundle_sha256', 'archive_sha256', 'source_commit', 'source_tree'):
        size = 40 if key in ('source_commit', 'source_tree') else 64
        _native_need(type(d[key]) is str and re.fullmatch('[0-9a-f]{'+str(size)+'}', d[key]) is not None)
    _native_need(type(d['source_version']) is int and 1 <= d['source_version'] <= 2147483647
        and type(d['byte_size']) is int and 0 < d['byte_size'] <= NATIVE_LIMIT)
    refs = d['material_refs']
    _native_need(type(refs) is list and 1 <= len(refs) <= 8)
    ids, total = set(), 0
    for ref in refs:
        _native_need(type(ref) is dict and set(ref) == {'material_id', 'sha256', 'byte_size', 'path'})
        mid = ref['material_id']
        _native_need(type(mid) is str and str(UUID(mid)) == mid and UUID(mid).int != 0 and mid not in ids)
        ids.add(mid)
        _native_need(type(ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}', ref['sha256']) is not None
            and type(ref['byte_size']) is int and 0 < ref['byte_size'] <= 1048576
            and ref['path'] in ('Resources/Input/'+mid+'.txt', 'Resources/Input/'+mid+'.json'))
        total += ref['byte_size']
    _native_need(total <= 4194304)
    return d


def _native_path(path):
    _native_need(type(path) is str and 0 < len(path.encode('utf-8')) <= 512
        and not path.startswith('/') and re.fullmatch(r'[A-Za-z0-9_./-]+', path) is not None)
    for part in path.split('/'):
        _native_need(part not in ('', '.', '..') and not part.endswith('.')
            and re.fullmatch(r'(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', part) is None)
    return path


def native_tree(files):
    """Git object/tree hashing over the complete regular-file current tree."""
    tree = {}
    aliases = set()
    for path, (mode, raw) in files.items():
        _native_path(path)
        _native_need(path.casefold() not in aliases and mode in ('100644', '100755') and type(raw) is bytes)
        aliases.add(path.casefold())
        parts = path.split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            _native_need(type(node) is dict)
        _native_need(parts[-1] not in node)
        node[parts[-1]] = (mode, hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).digest())
    def digest(node):
        entries = []
        for name, item in node.items():
            directory = type(item) is dict
            mode, oid = ('40000', digest(item)) if directory else item
            entries.append(((name+('/' if directory else '')).encode(), mode.encode()+b' '+name.encode()+b'\0'+oid))
        raw = b''.join(value for _, value in sorted(entries))
        return hashlib.sha1(b'tree '+str(len(raw)).encode()+b'\0'+raw).digest()
    return digest(tree).hex()


def native_files_policy(files, descriptor):
    _native_need(0 < len(files) <= 1000 and sum(len(v[1]) for v in files.values()) <= NATIVE_LIMIT)
    _native_need(native_tree(files) == descriptor['source_tree'])
    bundle = 'private.leaf.project.p'+descriptor['project_id'].replace('-', '')
    expected = NATIVE_PROJECT_SPEC.replace('${bundleId}', bundle).encode()
    _native_need(files.get('project.yml') == ('100644', expected)
        and files.get('Sources/LeafProjectApp.swift') == ('100644', NATIVE_APP_SOURCE.encode()))
    content = files.get('Sources/ContentView.swift')
    _native_need(content is not None and content[0] == '100644' and bool(content[1].strip()))
    total = 0
    for path, (mode, raw) in files.items():
        _native_need(len(raw) <= NATIVE_LIMIT)
        if path.startswith('Sources/'):
            _native_need(mode == '100644' and path.endswith('.swift') and len(raw) <= 1048576)
            text = raw.decode('utf-8')
            _native_need(path == 'Sources/LeafProjectApp.swift' or re.search(r'@\s*main\b', text) is None)
        if path.startswith('Resources/Input/'):
            _native_need(mode == '100644' and len(raw) <= 1048576)
            total += len(raw)
    _native_need(total <= 4194304)
    if descriptor['source_operation'] == 'edit':
        _native_need({p for p in files if p.startswith('Resources/Input/')} ==
                     {ref['path'] for ref in descriptor['material_refs']})
    for ref in descriptor['material_refs']:
        mode, raw = files.get(ref['path'], (None, None))
        _native_need(mode == '100644' and type(raw) is bytes and len(raw) == ref['byte_size']
            and hashlib.sha256(raw).hexdigest() == ref['sha256'])
        raw.decode('utf-8')


def native_archive(files, descriptor):
    native_files_policy(files, descriptor)
    _native_need(NATIVE_MANIFEST not in files)
    manifest = {'schema': 'leaf.native-sdk-tree.v1', 'source': native_identity(descriptor),
                'files': [{'path': p, 'mode': mode,
                    'blob': hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}
                    for p, (mode, raw) in sorted(files.items())]}
    # Archive digest is an outer descriptor field, never circular manifest input.
    manifest['source'].pop('archive_sha256')
    members = dict(files, **{NATIVE_MANIFEST: ('100644', _native_json(manifest))})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for path, (mode, raw) in sorted(members.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if mode == '100755' else 0o644)) << 16
            archive.writestr(info, raw)
    raw = output.getvalue()
    _native_need(0 < len(raw) <= NATIVE_LIMIT)
    return raw


def verify_native_archive(raw, descriptor):
    d = native_descriptor(descriptor, descriptor['org_id'], descriptor['project_id'])
    _native_need(type(raw) is bytes and len(raw) == d['byte_size'] and len(raw) <= NATIVE_LIMIT
        and hashlib.sha256(raw).hexdigest() == d['archive_sha256'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        _native_need(1 < len(infos) <= 1001 and len({i.filename for i in infos}) == len(infos)
            and sum(i.file_size for i in infos) <= NATIVE_LIMIT and not archive.comment)
        files = {}
        for i in infos:
            _native_path(i.filename)
            _native_need(not i.is_dir() and not i.flag_bits & 1 and not i.extra and not i.comment
                and i.compress_type == zipfile.ZIP_STORED and i.file_size <= NATIVE_LIMIT
                and stat.S_IFMT(i.external_attr >> 16) == stat.S_IFREG)
            mode = '100755' if i.external_attr >> 16 & 0o111 else '100644'
            if i.filename != NATIVE_MANIFEST:
                files[i.filename] = (mode, archive.read(i))
        native_files_policy(files, d)
        _native_need(native_archive(files, d) == raw)
    return files


def _native_candidate(org, project, actor, request_id, *, verify_originals=False, retained_refs=None):
    import browser_project_flow as flow
    import project_reference_materials as originals
    from leaf_platform import project_reference_materials as metadata
    project_lifecycle.require_project_role(org, project, actor, write=True)
    current = (org, project, actor)
    authority = flow.repository(current)
    row = flow._read(authority, _uuid(request_id)) if authority else None
    _native_need(type(row) is dict and row.get('state') == 'published')
    flow._membership(row, authority, current, NATIVE_PROFILE)
    receipt = flow._receipt(row, authority)
    # Membership has verified this exact original turn and full canonical body.
    _, sid, tid, _ = row['idempotency_key'].split(':')
    events = flow.session_store.events_after(sid, 0, 10000)
    markers = [e['data'] for e in events if e['turn_id'] == tid and e['type'] == 'browser_source_dispatch']
    _native_need(len(markers) == 1)
    request = markers[0]['request']
    operation = request['source_operation']
    _native_need(operation in ('create', 'edit')
        and (1 if operation == 'create' else 0) <= len(request['material_refs']) <= 8)
    identity = dict(schema=NATIVE_SCHEMA, target='ios-simulator', source_profile=NATIVE_PROFILE,
        org_id=org, project_id=project, actor_binding_id=actor, repo_key=authority['repo_key'],
        source_request_id=request_id, source_operation=operation, source_version=row['version'],
        receipt_digest=row['receipt_digest'], source_commit=receipt.staged_head_commit,
        source_tree=receipt.staged_tree)
    material_refs = request['material_refs']
    if operation == 'edit':
        if retained_refs is None:
            # Initial capture derives provenance from the exact current-main tree.
            files = _native_bundle_files(_native_export(identity), identity)
            retained = {}
            for path, (_, raw) in sorted(files.items()):
                if path.startswith('Resources/Input/'):
                    name = path.removeprefix('Resources/Input/')
                    mid, suffix = name.rsplit('.', 1)
                    _native_need(suffix in ('txt', 'json') and _uuid(mid) == mid and mid not in retained)
                    retained[mid] = dict(material_id=mid, sha256=hashlib.sha256(raw).hexdigest())
        else:
            # Rechecks authorize each saved ref again without exporting on every
            # heartbeat. Archive policy binds the complete input set to the tree;
            # prepare/read still require the independent current-main exporter.
            retained = {ref['material_id']: dict(material_id=ref['material_id'], sha256=ref['sha256'])
                        for ref in retained_refs}
        for ref in material_refs:
            _native_need(retained.get(ref['material_id']) == ref)
        material_refs = list(retained.values())
    _native_need(1 <= len(material_refs) <= 8)
    refs, total = [], 0
    for ref in material_refs:
        material = metadata.get_material(org, project, actor, ref['material_id'])
        _native_need(type(material) is dict and material.get('org_id') == org
            and material.get('project_id') == project and material.get('actor_binding_id') == actor
            and material.get('material_id') == ref['material_id'] and material.get('sha256') == ref['sha256']
            and material.get('media_type') in ('text/plain', 'application/json')
            and type(material.get('byte_size')) is int and 0 < material['byte_size'] <= 1048576)
        total += material['byte_size']
        _native_need(total <= 4194304)
        if verify_originals:
            original = originals.read_content(org, project, material)
            _native_need(type(original) is bytes and len(original) == material['byte_size']
                and hashlib.sha256(original).hexdigest() == ref['sha256']
                and metadata.get_material(org, project, actor, ref['material_id']) == material)
            original.decode('utf-8')
        suffix = '.json' if material['media_type'] == 'application/json' else '.txt'
        refs.append(dict(ref, byte_size=material['byte_size'],
                         path='Resources/Input/'+ref['material_id']+suffix))
    project_lifecycle.require_project_role(org, project, actor, write=True)
    _native_need(flow.repository(current) == authority and flow._read(authority, request_id) == row)
    return dict(identity, material_refs=refs)


def _native_export(identity):
    from project_repository_source import export_project_source_bundle
    value = export_project_source_bundle(identity['org_id'], identity['org_id'], identity['project_id'],
        identity['source_commit'], identity['source_tree'])
    _native_need(type(value) is dict and type(value.get('bundle')) is bytes
        and 0 < len(value['bundle']) <= 67108864
        and value.get('source_commit') == identity['source_commit']
        and value.get('source_tree') == identity['source_tree']
        and value.get('size_bytes') == len(value['bundle'])
        and value.get('bundle_sha256') == hashlib.sha256(value['bundle']).hexdigest())
    return value


def _native_bundle_files(export, identity):
    """Consume the existing refs/heads/main producer without checking out code."""
    allowed = ('PATH', 'SystemRoot', 'SYSTEMROOT', 'TEMP', 'TMP')
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    with tempfile.TemporaryDirectory(prefix='leaf-native-sdk-source-') as directory:
        directory = Path(directory)
        bundle = directory/'source.bundle'
        bundle.write_bytes(export['bundle'])
        def git(*args, limit=NATIVE_LIMIT):
            command = ['git', '-c', 'core.hooksPath='+os.devnull, '-c', 'protocol.allow=never',
                       '-C', str(directory), *args]
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.DEVNULL, timeout=30)
                _native_need(result.returncode == 0)
                output.seek(0)
                raw = output.read(limit+1)
                _native_need(len(raw) <= limit)
                return raw
        git('init', '--bare', 'verified.git', limit=16384)
        git('--git-dir=verified.git', 'bundle', 'verify', str(bundle), limit=16384)
        expected = (identity['source_commit']+' refs/heads/main\n').encode()
        _native_need(git('--git-dir=verified.git', 'bundle', 'list-heads', str(bundle), limit=16384) == expected)
        git('--git-dir=verified.git', '-c', 'protocol.file.allow=always', 'fetch', '--no-tags',
            '--no-write-fetch-head', str(bundle), 'refs/heads/main', limit=16384)
        commit = identity['source_commit']
        _native_need(git('--git-dir=verified.git', 'rev-parse', commit+'^{tree}', limit=100).strip().decode()
                     == identity['source_tree'])
        listing = git('--git-dir=verified.git', 'ls-tree', '-rz', '--full-tree', commit, limit=1048576)
        entries = listing.rstrip(b'\0').split(b'\0')
        _native_need(0 < len(entries) <= 1000)
        files, total = {}, 0
        for entry in entries:
            metadata, name = entry.split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split(' ')
            path = _native_path(name.decode('utf-8'))
            _native_need(mode in ('100644', '100755') and kind == 'blob' and path not in files
                and re.fullmatch('[0-9a-f]{40}', oid) is not None)
            size = int(git('--git-dir=verified.git', 'cat-file', '-s', oid, limit=32))
            _native_need(0 <= size <= NATIVE_LIMIT and total+size <= NATIVE_LIMIT)
            raw = git('--git-dir=verified.git', 'cat-file', 'blob', oid, limit=size)
            _native_need(len(raw) == size)
            files[path] = (mode, raw)
            total += size
        _native_need(native_tree(files) == identity['source_tree'])
        return files


def prepare_native_source(org_id, project_id, actor_id, *, source_request_id):
    org, project, actor = map(_uuid, (org_id, project_id, actor_id))
    identity = _native_candidate(org, project, actor, _uuid(source_request_id), verify_originals=True)
    exported = _native_export(identity)
    files = _native_bundle_files(exported, identity)
    descriptor = dict(identity, bundle_sha256=exported['bundle_sha256'],
                      archive_sha256='0'*64, byte_size=1)
    raw = native_archive(files, descriptor)
    descriptor.update(archive_sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    native_descriptor(descriptor, org, project)
    verify_native_archive(raw, descriptor)
    _native_need(_native_candidate(org, project, actor, source_request_id,
        retained_refs=identity['material_refs']) == identity)
    import write_loop
    try:
        backend = write_loop.upload_backend_for_tenant(org)
        archive, manifest = _keys(descriptor)
        backend.put_if_absent_or_verify(archive, raw)
        backend.put_if_absent_or_verify(manifest, _manifest(descriptor))
        _read_persisted(backend, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    recheck_native_source(org, project, actor, descriptor)
    return descriptor


def recheck_native_source(org, project, actor, descriptor):
    descriptor = native_descriptor(descriptor, org, project)
    _native_need(descriptor['actor_binding_id'] == actor)
    identity = _native_candidate(org, project, actor, descriptor['source_request_id'],
                                 retained_refs=descriptor['material_refs'])
    _native_need(all(descriptor[k] == v for k, v in identity.items()))
    return identity


def read_native_source(org, project, actor, descriptor):
    identity = recheck_native_source(org, project, actor, descriptor)
    # Existing exporter is the current-main producer. Do not compare repacked
    # Git bytes with the old pack digest when immutable commit/tree agree.
    _native_export(identity)
    import write_loop
    try:
        raw = _read_persisted(write_loop.upload_backend_for_tenant(org), descriptor)
        verify_native_archive(raw, descriptor)
    except _PRIVATE_ERRORS:
        raise
    except Exception:
        raise SourceStoreUnavailable('Preview source storage is unavailable.') from None
    recheck_native_source(org, project, actor, descriptor)
    return raw


def validate_native_artifact(value, descriptor):
    _native_need(type(value) is dict and set(value) == {'source', 'bundle_id', 'sdk',
        'app_tree_sha256', 'file_count', 'xcodegen_exit_code', 'xcode_version', 'xcodegen_version', 'sdk_version'})
    _native_need(value['source'] == native_identity(descriptor) and value['sdk'] == 'iphonesimulator'
        and value['bundle_id'] == 'private.leaf.project.p'+descriptor['project_id'].replace('-', '')
        and type(value['app_tree_sha256']) is str and re.fullmatch('[0-9a-f]{64}', value['app_tree_sha256']) is not None
        and type(value['file_count']) is int and 0 < value['file_count'] <= 10000
        and type(value['xcodegen_exit_code']) is int and value['xcodegen_exit_code'] == 0)
    for key in ('xcode_version', 'xcodegen_version', 'sdk_version'):
        text = value[key]
        _native_need(type(text) is str and 0 < len(text) <= 256
            and all(c in '\n\r\t' or 32 <= ord(c) < 127 for c in text))
    return value


def validate_native_identity(value):
    from uuid import UUID
    fields = {'schema', 'source_profile', 'repo_key', 'source_request_id', 'source_version',
              'receipt_digest', 'source_commit', 'source_tree', 'archive_sha256'}
    _native_need(type(value) is dict and set(value) == fields
        and value['schema'] == NATIVE_SCHEMA and value['source_profile'] == NATIVE_PROFILE
        and type(value['source_version']) is int and 1 <= value['source_version'] <= 2147483647)
    for key in ('repo_key', 'source_request_id'):
        _native_need(type(value[key]) is str and str(UUID(value[key])) == value[key] and UUID(value[key]).int != 0)
    for key in ('receipt_digest', 'source_commit', 'source_tree', 'archive_sha256'):
        size = 40 if key in ('source_commit', 'source_tree') else 64
        _native_need(type(value[key]) is str and re.fullmatch('[0-9a-f]{'+str(size)+'}', value[key]) is not None)
    return value
