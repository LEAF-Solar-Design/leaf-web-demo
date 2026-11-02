"""Native adapter proofs over the actual original reader and protected HTTP boundary."""
import base64
import uuid
import pytest
import browser_project_authoring as authoring
import native_project_authoring as native
from test_browser_project_authoring import lane, PROJECT, MATERIAL, RAW, BASE, ACTOR


@pytest.mark.parametrize('count', [0, 1, 8])
def test_native_edit_consumer_exact_binding_and_helper_only_receipt(lane, count):
    from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
    lane.request.update(source_operation='edit', expected_base_commit=BASE,
        material_refs=[lane.add(b'original', 'text/plain', str(uuid.uuid4())) for _ in range(count)])
    def helper(result):
        result['receipt']['changed_paths'] = ['Sources/RecipeModel.swift']
        result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(result['receipt']))
        return result
    lane.response_mutation = helper
    result = native.author(lane.tenant, PROJECT, lane.request)
    body = lane.posts[0]
    assert len(lane.posts) == 1 and len(lane.initializations) == 1
    assert body['source_operation'] == 'edit' and body['expected_base_commit'] == BASE
    assert body['source_profile'] == native.SOURCE_PROFILE and body['actor_binding_id'] == ACTOR
    assert body['requirements'] == lane.request['requirements']
    assert all(body[key] == value for key, value in lane.authority.items())
    assert len(body['material']) == count
    assert all(base64.b64decode(item['product_b64']) == b'original' for item in body['material'])
    assert result['receipt']['changed_paths'] == ['Sources/RecipeModel.swift']
    assert result['receipt']['operation'] == 'edit' and 'source_profile' not in result['receipt']


@pytest.mark.parametrize('kind', ['stale-base', 'missing-repo', 'actor', 'project', 'per-file', 'aggregate'])
def test_native_edit_consumer_refuses_stale_target_or_unauthorized_originals(lane, kind):
    lane.request.update(source_operation='edit', expected_base_commit=BASE)
    if kind == 'stale-base':
        lane.request['expected_base_commit'] = 'f'*40
    elif kind == 'missing-repo':
        lane.authority = None
    elif kind == 'actor':
        lane.rows[MATERIAL]['actor_binding_id'] = str(uuid.uuid4())
    elif kind == 'project':
        lane.rows[MATERIAL]['project_id'] = str(uuid.uuid4())
    elif kind == 'per-file':
        lane.rows[MATERIAL]['byte_size'] = 1048577
    else:
        lane.request['material_refs'] = [lane.add(b'x'*1048576, 'text/plain', str(uuid.uuid4())) for _ in range(5)]
    with pytest.raises(authoring.AuthoringError):
        native.author(lane.tenant, PROJECT, lane.request)
    assert not lane.posts
    if kind != 'stale-base':
        assert not lane.initializations


@pytest.mark.parametrize('kind', ['digest', 'witness', 'actor', 'base', 'profile'])
def test_native_edit_consumer_helper_only_receipt_still_checks_binding(lane, kind):
    from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
    lane.request.update(source_operation='edit', expected_base_commit=BASE, material_refs=[])
    def tamper(result):
        receipt = result['receipt']
        receipt['changed_paths'] = ['Sources/RecipeModel.swift']
        if kind == 'actor':
            receipt['actor_binding_id'] = str(uuid.uuid4())
        elif kind == 'base':
            receipt['base_commit'] = 'f'*40
        elif kind == 'profile':
            receipt['source_profile'] = native.SOURCE_PROFILE
            return result
        result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(receipt))
        if kind == 'digest':
            result['receiptDigest'] = 'f'*64
        elif kind == 'witness':
            result['witness']['staged_tree'] = 'f'*40
        return result
    lane.response_mutation = tamper
    with pytest.raises(authoring.AuthoringError) as caught:
        native.author(lane.tenant, PROJECT, lane.request)
    assert caught.value.code == 'source_changed' and len(lane.posts) == 1


def test_native_edit_consumer_create_fixed_profile_full_original_and_actual_receipt(lane):
    result = native.author(lane.tenant, PROJECT, lane.request)
    assert len(lane.posts) == 1
    assert lane.posts[0]['source_profile'] == native.SOURCE_PROFILE
    assert lane.posts[0]['source_operation'] == 'create'
    assert base64.b64decode(lane.posts[0]['material'][0]['product_b64']) == RAW
    assert 'source_profile' not in result['receipt']
    assert result['receipt']['operation'] == 'edit'


@pytest.mark.parametrize('change', [dict(source_operation='edit'), dict(source_operation='delete'),
    dict(material_refs=[]), dict(source_profile='web-next-static-v1'), dict(expected_base_commit='bad'),
    dict(requirements=''), dict(requirements='x'*16385)])
def test_native_edit_consumer_invalid_request_has_no_effect(lane, change):
    lane.request.update(change)
    with pytest.raises(authoring.AuthoringError):
        native.author(lane.tenant, PROJECT, lane.request)
    assert not lane.posts and not lane.initializations and not lane.reads


@pytest.mark.parametrize('profile', ['unknown', None, 1])
def test_unknown_internal_profile_refuses_before_effect(lane, profile):
    with pytest.raises(authoring.AuthoringError):
        authoring.author(lane.tenant, PROJECT, lane.request, source_profile=profile)
    assert not lane.posts and not lane.initializations


@pytest.mark.parametrize('kind', ['per-file', 'aggregate', 'actor', 'hash'])
def test_native_original_limits_and_authority(lane, kind):
    if kind == 'per-file':
        lane.rows[MATERIAL]['byte_size'] = 1048577
    elif kind == 'aggregate':
        lane.request['material_refs'] = [lane.add(b'x'*1048576, 'text/plain', str(uuid.uuid4())) for _ in range(5)]
    elif kind == 'actor':
        lane.rows[MATERIAL]['actor_binding_id'] = str(uuid.uuid4())
    else:
        lane.rows[MATERIAL]['sha256'] = 'f'*64
    with pytest.raises(authoring.AuthoringError):
        native.author(lane.tenant, PROJECT, lane.request)
    assert not lane.posts and not lane.initializations


def test_native_exact_four_mib_is_complete(lane):
    raw = b'x'*1048576
    lane.request['material_refs'] = [lane.add(raw, 'text/plain', str(uuid.uuid4())) for _ in range(4)]
    native.author(lane.tenant, PROJECT, lane.request)
    assert sum(m['size_bytes'] for m in lane.posts[0]['material']) == 4194304
    assert all(base64.b64decode(m['product_b64']) == raw for m in lane.posts[0]['material'])


def test_native_edit_consumer_create_wrong_profile_product_witness_is_refused(lane):
    def wrong(result):
        from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
        result['receipt']['changed_paths'] = ['app/page.tsx']
        result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(result['receipt']))
        return result
    lane.response_mutation = wrong
    with pytest.raises(authoring.AuthoringError):
        native.author(lane.tenant, PROJECT, lane.request)
