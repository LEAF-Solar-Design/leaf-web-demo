"""Native adapter proofs over the actual original reader and protected HTTP boundary."""
import base64
import uuid
import pytest
import browser_project_authoring as authoring
import native_project_authoring as native
from test_browser_project_authoring import lane, PROJECT, MATERIAL, RAW


def test_native_fixed_profile_full_original_and_actual_receipt(lane):
    result = native.author(lane.tenant, PROJECT, lane.request)
    assert len(lane.posts) == 1
    assert lane.posts[0]['source_profile'] == native.SOURCE_PROFILE
    assert lane.posts[0]['source_operation'] == 'create'
    assert base64.b64decode(lane.posts[0]['material'][0]['product_b64']) == RAW
    assert 'source_profile' not in result['receipt']
    assert result['receipt']['operation'] == 'edit'


@pytest.mark.parametrize('change', [dict(source_operation='edit', expected_base_commit='a'*40),
    dict(material_refs=[]), dict(source_profile='web-next-static-v1'), dict(expected_base_commit='bad'),
    dict(requirements=''), dict(requirements='x'*16385)])
def test_native_invalid_request_has_no_effect(lane, change):
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


def test_wrong_profile_product_witness_is_refused(lane):
    def wrong(result):
        from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
        result['receipt']['changed_paths'] = ['app/page.tsx']
        result['receiptDigest'] = staged_receipt_digest(parse_staged_receipt(result['receipt']))
        return result
    lane.response_mutation = wrong
    with pytest.raises(authoring.AuthoringError):
        native.author(lane.tenant, PROJECT, lane.request)
