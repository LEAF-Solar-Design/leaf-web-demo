"""Authorized reader/author adapter with inert private storage and transport."""
import base64
import copy
import hashlib
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

import browser_project_authoring as authoring
import project_reference_materials as originals
from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest

ORG, PROJECT, ACTOR, REPO, EDIT, MATERIAL, LEASE = [str(uuid.uuid4()) for _ in range(7)]
BASE, HEAD, TREE = 'a' * 40, 'b' * 40, 'c' * 40
RAW = b'{"recipe":"complete original","steps":[1,2,3],"tail":"kept"}\n'


@pytest.fixture
def lane(monkeypatch):
    tenant = authoring.deps.TenantContext(ORG, subject='verified-subject')
    state = SimpleNamespace(revoked=False, cancelled=False, reads=[], posts=[], initializations=[],
                            raws={}, rows={}, response_status=200, response_mutation=lambda value: value)
    authority = dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)
    state.authority = authority
    binding = SimpleNamespace(binding_id=uuid.UUID(ACTOR), platform_tenant_id=uuid.UUID(ORG))

    def role(actual, project, *, write, binding):
        assert actual is tenant and str(project) == PROJECT and write is True
        if state.revoked:
            raise PermissionError('revoked')
        return ORG

    monkeypatch.setattr(authoring.platform_link, 'resolve_caller_binding', lambda actual: binding)
    monkeypatch.setattr(authoring.platform_link, 'require_project_access', role)
    authoring.platform_link._ensure_platform_package()
    from leaf_platform import project_reference_materials as metadata

    def get_material(org, project, actor, material_id):
        assert (org, project, actor) == (ORG, PROJECT, ACTOR)
        if material_id not in state.rows:
            raise LookupError('unavailable')
        return copy.deepcopy(state.rows[material_id])

    monkeypatch.setattr(metadata, 'get_material', get_material)

    def get(key):
        state.reads.append(key)
        if getattr(state, 'revoke_on_read', False):
            state.revoked = True
        if getattr(state, 'cancel_on_read', False):
            state.cancelled = True
        return state.raws[key]

    def backend(actual):
        assert actual == ORG
        return SimpleNamespace(get=get)

    monkeypatch.setitem(sys.modules, 'write_loop', SimpleNamespace(upload_backend_for_tenant=backend))
    # read_content itself is intentionally real: it derives the private key and
    # verifies the original byte count and digest before this adapter sees bytes.
    assert originals.read_content is authoring.originals.read_content
    monkeypatch.setattr(authoring.platform_link, 'platform_store', lambda: SimpleNamespace(
        resolve_project_repository_authority=lambda *args: copy.deepcopy(state.authority)))

    def initialize(*args):
        assert args[:3] == (ORG, ORG, PROJECT)
        state.initializations.append(args)
        return dict(source_commit=BASE, source_tree=TREE, seed_digest=hashlib.sha256(args[3].encode()).hexdigest(), replayed=False)

    monkeypatch.setattr(authoring.source, 'initialize_project_source', initialize)
    monkeypatch.setattr(authoring.broker_client, 'harness_headers', lambda: {'X-Harness-Secret': 'fixture-only'})
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'https://harness.invalid')

    def post(url, *, data, headers, timeout, allow_redirects):
        assert url == 'https://harness.invalid/internal/project-repository-source/author'
        assert headers == {'X-Harness-Secret': 'fixture-only', 'X-Tenant-Id': ORG, 'Content-Type': 'application/json'}
        assert timeout == (10, 240) and allow_redirects is False
        body = json.loads(data)
        state.posts.append(body)
        receipt = dict(contract='leaf.project-repository-edit.v2', edit_id=EDIT, state='staged',
            operation='edit', source_edit_id=None, actor_binding_id=ACTOR, **authority,
            writer_lease_id=LEASE, writer_lease_generation=1, base_commit=BASE,
            staged_head_commit=HEAD, staged_tree=TREE,
            changed_paths=['Sources/ContentView.swift'] if body['source_profile'] == authoring.NATIVE_PROFILE else ['app/page.tsx'],
            diff_digest='d' * 64, instruction_digest=body['instruction_digest'],
            idempotency_key=body['idempotency_key'])
        result = dict(witness=dict(base_commit=BASE, staged_head_commit=HEAD, staged_tree=TREE,
            private_ref='refs/leaf/changes/' + EDIT), receipt=receipt,
            receiptDigest=staged_receipt_digest(parse_staged_receipt(receipt)), version=1)
        result = state.response_mutation(result)
        if getattr(state, 'cancel_on_post', False):
            state.cancelled = True
        if getattr(state, 'rebind_on_post', False):
            state.authority['repo_key'] = str(uuid.uuid4())
        return SimpleNamespace(status_code=state.response_status, json=lambda **kwargs: result, close=lambda: None)

    monkeypatch.setattr(authoring.requests, 'post', post)

    def add(raw=RAW, media_type='application/json', material_id=MATERIAL):
        digest = hashlib.sha256(raw).hexdigest()
        state.rows[material_id] = dict(material_id=material_id, org_id=ORG, project_id=PROJECT,
            actor_binding_id=ACTOR, name='caller-name-is-not-a-path.json', media_type=media_type,
            byte_size=len(raw), sha256=digest)
        state.raws[originals.object_key(ORG, PROJECT, digest)] = raw
        return {'material_id': material_id, 'sha256': digest}

    state.add = add
    state.tenant = tenant
    state.request = dict(edit_id=EDIT, requirements='Use the original data, never replace its values.',
                         idempotency_key='request-one', source_operation='create', material_refs=[add()])
    return state


def call(lane):
    return authoring.author(lane.tenant, PROJECT, lane.request, cancelled=lambda: lane.cancelled)


def test_complete_original_reaches_hash_bound_asset_not_prompt(lane):
    result = call(lane)
    assert result['receipt']['staged_head_commit'] == HEAD
    body = lane.posts[0]
    material = body['material'][0]
    assert set(body) == {'tenant_id', 'organization_id', 'project_id', 'repo_key', 'edit_id', 'actor_binding_id',
                         'expected_base_commit', 'instruction_digest', 'idempotency_key', 'commit_message',
                         'requirements', 'material', 'source_profile', 'source_operation'}
    assert material['path'] == MATERIAL + '.json'
    assert base64.b64decode(material['product_b64']) == RAW
    assert material['sha256'] == hashlib.sha256(RAW).hexdigest()
    assert material['size_bytes'] == len(RAW)
    assert body['requirements'] == lane.request['requirements'] and 'complete original' not in body['requirements']
    assert body['source_profile'] == 'web-next-static-v1'
    assert lane.reads == [originals.object_key(ORG, PROJECT, material['sha256'])]


@pytest.mark.parametrize('field', ['tenant_id', 'organization_id', 'actor_binding_id', 'repo_key', 'path', 'key', 'url'])
def test_caller_cannot_supply_authority_or_storage_selectors(lane, field):
    lane.request[field] = 'malicious-caller-value'
    with pytest.raises(authoring.AuthoringError, match='Invalid'):
        call(lane)
    assert not lane.reads and not lane.posts and not lane.initializations


@pytest.mark.parametrize('kind', ['missing', 'foreign-project', 'foreign-org', 'stale'])
def test_missing_cross_project_and_stale_references_refuse_before_read(lane, kind):
    if kind == 'missing':
        lane.rows.clear()
    elif kind == 'stale':
        lane.request['material_refs'][0]['sha256'] = 'f' * 64
    else:
        lane.rows[MATERIAL]['project_id' if kind == 'foreign-project' else 'org_id'] = str(uuid.uuid4())
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.code == ('source_changed' if kind == 'stale' else 'unavailable')
    assert not lane.reads and not lane.posts and not lane.initializations


@pytest.mark.parametrize('kind', ['revoked', 'revoke_on_read', 'cancel_on_read', 'cancel_on_post'])
def test_revocation_and_cancellation_prevent_downstream_success(lane, kind):
    setattr(lane, kind, True)
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.code == ('access_denied' if 'revok' in kind else 'cancelled')
    if kind != 'cancel_on_post':
        assert not lane.posts


def test_video_is_not_claimed_consumed(lane):
    lane.rows[MATERIAL]['media_type'] = 'video/quicktime'
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.code == 'unsupported_material'
    assert not lane.reads and not lane.posts


@pytest.mark.parametrize('aggregate', [False, True])
def test_caps_are_checked_before_storage_reads(lane, aggregate):
    lane.rows[MATERIAL]['byte_size'] = authoring.MATERIAL_LIMIT + 1
    if aggregate:
        lane.rows[MATERIAL]['byte_size'] = authoring.MATERIAL_LIMIT
        lane.request['material_refs'].append(lane.add(b'x', 'text/plain', str(uuid.uuid4())))
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.status == 413
    assert not lane.reads and not lane.posts


def test_exact_text_limit_is_not_truncated(lane):
    raw = b'x' * authoring.MATERIAL_LIMIT
    lane.request['material_refs'] = [lane.add(raw, 'text/plain')]
    call(lane)
    asset = lane.posts[0]['material'][0]
    assert asset['size_bytes'] == len(raw) and base64.b64decode(asset['product_b64']) == raw


def test_corrupt_stored_original_never_reaches_author(lane):
    key = next(iter(lane.raws))
    lane.raws[key] = b'different bytes'
    with pytest.raises(authoring.AuthoringError):
        call(lane)
    assert not lane.posts


def test_brief_only_and_edit_base_witness(lane):
    lane.request['material_refs'] = []
    call(lane)
    assert lane.posts[0]['material'] == [] and not lane.reads
    lane.request.update(source_operation='edit', expected_base_commit='e' * 40)
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.code == 'source_changed' and len(lane.posts) == 1


@pytest.mark.parametrize('kind', ['digest', 'actor', 'witness', 'version', 'rebind'])
def test_stale_or_forged_internal_result_is_not_accepted(lane, kind):
    def mutate(result):
        if kind == 'digest':
            result['receiptDigest'] = '0' * 64
        elif kind == 'actor':
            result['receipt']['actor_binding_id'] = str(uuid.uuid4())
            result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(result['receipt']))
        elif kind == 'witness':
            result['witness']['private_ref'] = 'refs/heads/main'
        elif kind == 'version':
            result['version'] = True
        return result
    lane.response_mutation = mutate
    lane.rebind_on_post = kind == 'rebind'
    with pytest.raises(authoring.AuthoringError) as error:
        call(lane)
    assert error.value.code == 'source_changed'


def test_default_web_and_explicit_web_transport_bytes_match(lane):
    call(lane)
    expected = copy.deepcopy(lane.posts[0])
    authoring.author(lane.tenant, PROJECT, lane.request, source_profile=authoring.WEB_PROFILE)
    assert lane.posts[1] == expected
    assert expected['commit_message'] == 'Author browser project'
    assert expected['source_profile'] == 'web-next-static-v1'
