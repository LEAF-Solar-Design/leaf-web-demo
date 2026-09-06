"""Producer-shaped product wire fixtures shared with the PostgreSQL publication proof."""
import base64
import copy
import hashlib
import json
import uuid

import pytest

import campaign_product_execution as product


def native_output(source, paths=None):
    paths = paths or ['recipes.py']
    prefix = f"leaf-results/{source['run_id']}/{source['leaf_id']}/{source['remote_fencing_token']}/"

    def entry(path, raw):
        return dict(path=path, key=prefix + path, sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw), product_b64=base64.b64encode(raw).decode())

    files = [dict(entry(path, b'print("recipes")\n'), before_sha256=None) for path in sorted(paths)]
    before = {path: None for path in paths}
    after = {file['path']: file['sha256'] for file in files}
    binding = dict(requested_sha=source['source_ref'], initial_head=source['source_ref'],
                   fleet_runtime_sha='a' * 40, bound_input=None)

    def meta(stage):
        model = 'codex-model' if stage == 'edit' else 'claude-model'
        return dict(run_id=source['run_id'], milestone_id='pair-' + stage, attempt=1,
                    requested_model=model, observed_model=model, cli_version='1', cli_path='/native/cli',
                    usage=dict(cost_usd=0.01, tokens={}, known=True), evidence_kind='native_cli')

    edit = dict(meta('edit'), base_sha='d' * 40, result_sha='e' * 40,
                changed_paths=paths, before=before, after=after)
    accept = dict(meta('accept'), argv=['python', 'recipes.py'], rc=0, verdict='accept', log_sha256='a' * 64)
    receipts = {name: entry(product.RECEIPTS[name], json.dumps(doc).encode()) for name, doc in
                [('source_binding', binding), ('edit', edit), ('accept', accept)]}
    final = dict(binding, schema_version=1, run_id=source['run_id'], leaf_id=source['leaf_id'],
        fencing_token=source['remote_fencing_token'], plan_sha256='b' * 64,
        stage_receipt_digests={'findings/pair/plan.json': 'b' * 64,
            product.RECEIPTS['edit']: receipts['edit']['sha256'],
            product.RECEIPTS['accept']: receipts['accept']['sha256']},
        calls=[meta(stage) for stage in ('plan', 'edit', 'accept')], acceptance=accept,
        owned_files_before=before, owned_files_after=after, evidence_kind='native_cli')
    receipts['final'] = entry(product.RECEIPTS['final'], json.dumps(final).encode())
    return dict(kind='files', **{k: source[k] for k in ('leaf_id', 'run_id', 'result_fingerprint',
        'task_id', 'payload_fingerprint', 'source_ref')}, fencing_token=source['remote_fencing_token'],
        base_sha=edit['base_sha'], result_sha=edit['result_sha'], changed_paths=paths,
        files=files, receipts=receipts)


@pytest.fixture
def wire():
    source = dict(task_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()), fence=1,
        leaf_id='vmc-' + '1' * 48, run_id='run-one', remote_fencing_token=7,
        result_fingerprint='f' * 64, payload_fingerprint='a' * 64, source_ref='b' * 40)
    task = dict(owned_paths=['recipes.py'], verify_command='python recipes.py')
    return native_output(source), task, source


def test_real_producer_shape_allows_internal_binding_commit(wire):
    output, task, saved = wire
    assert output['base_sha'] != saved['source_ref']
    assert product.validate_output(output, task, saved)['accept']['rc'] == 0


@pytest.mark.parametrize('command', ['sh -c true', 'cmd /c dir', 'pwsh -Command true',
    'python test.py && echo ok', 'python x > output', 'env python test.py', 'python $(bad)', 'python `bad`'])
def test_shell_argv_rejected(command):
    with pytest.raises(ValueError):
        product.verify_argv(command)


@pytest.mark.parametrize('path', ['../x', '/x', 'a\\b', '.git/config', '.leaf/source-seed.json',
    'PROMPT.md', 'findings/pair/edit.json', 'x:y', 'x%20', 'a//b', 'x\x00y'])
def test_protected_or_noncanonical_paths_rejected(path):
    with pytest.raises(ValueError):
        product.product_paths([path])


@pytest.mark.parametrize('change', ['hash', 'bytes', 'before', 'source', 'fingerprint', 'extra', 'size', 'bool', 'receipt'])
def test_invalid_output_rejected(wire, change):
    output, task, saved = copy.deepcopy(wire)
    if change == 'hash':
        output['files'][0]['sha256'] = '0' * 64
    elif change == 'bytes':
        output['files'][0]['product_b64'] += '\n'
    elif change == 'before':
        output['files'][0]['before_sha256'] = '0' * 64
    elif change == 'source':
        output['source_ref'] = '0' * 40
    elif change == 'fingerprint':
        output['payload_fingerprint'] = '0' * 64
    elif change == 'extra':
        output['approval'] = True
    elif change == 'size':
        output['files'][0]['size_bytes'] = product.FILE_LIMIT + 1
    elif change == 'bool':
        output['fencing_token'] = True
    else:
        output['receipts']['final']['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        product.validate_output(output, task, saved)


def test_duplicate_receipt_json_rejected(wire):
    output, task, saved = wire
    receipt = output['receipts']['source_binding']
    raw = base64.b64decode(receipt['product_b64']).replace(b'"bound_input":', b'"bound_input":null,"bound_input":')
    receipt.update(product_b64=base64.b64encode(raw).decode(), size_bytes=len(raw),
                   sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        product.validate_output(output, task, saved)
