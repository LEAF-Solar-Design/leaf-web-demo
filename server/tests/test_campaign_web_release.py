import copy
import hashlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import campaign_delivery_service as delivery
import campaign_release_service as runtime
import campaign_web_release as web
from routers import campaigns
from test_campaign_delivery_service import Store, Lifecycle

RAW = b'[{"name":"Panel A","watts":400},{"name":"Panel B","watts":410}]'


class WebFiles(Lifecycle):
    def put_project_file(self, *args, **kwargs):
        self.writes += 1
        self.files.append({'path': kwargs['path'], 'content': kwargs['content']})
        receipt = {'receipt_id': str(uuid.uuid4()), 'action': 'file_put', 'input_digest':
                   delivery.input_digest(kwargs['path'], kwargs['media_type'],
                     hashlib.sha256(kwargs['content'].encode()).hexdigest())}
        self.receipts.append(receipt)
        if self.interrupt:
            self.interrupt = False
            raise RuntimeError('lost response after commit')
        return {'receipt': receipt}


@pytest.fixture
def release(monkeypatch):
    files = WebFiles('input.json', RAW)
    store = Store('input.json', RAW)
    selected, recipe = web.compile_recipe(files.project_snapshot(), [])
    store.release['delivery_profile'] = 'web_tool'
    store.release['contract'].update(selected_artifact=selected, web_recipe=recipe,
        workflow=web.WORKFLOW, artifact_refs=['input.json'])
    store.release['contract']['required_checks'].append({'check_id': 'browser.download', 'stage': 'user_verification'})
    monkeypatch.setattr(runtime, '_STORE', store)
    monkeypatch.setattr(runtime, '_LIFECYCLE', files)
    scope = tuple(uuid.uuid4() for _ in range(3))
    monkeypatch.setattr(runtime, 'authority', lambda *args: scope)
    return store, files


def test_managed_release_real_browser_and_http_artifacts(release):
    store, files = release
    rid = store.release['release_id']
    result = runtime.advance('tenant', 'project', 'campaign', rid)
    assert result['release']['status'] == 'finished', result['stages']
    assert files.writes == 3
    assert all('/v1/' in f['path'] for f in files.files[1:])
    verification = next(s for s in store.stages if s['stage'] == 'user_verification')['evidence']
    assert any('real download' in action for action in verification['observations'])
    assert verification['browser_source_revision'] == store.release['contract']['selected_artifact']['sha256']
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[deps.require_tenant] = lambda: 'tenant'
    with TestClient(app) as client:
        url = f'/api/campaigns/campaign/releases/{rid}/artifacts/'
        page = client.get(url + web.HTML_NAME + '?project_id=project')
        assert page.status_code == 200, page.text
        assert page.content == web.static.render(RAW)
        assert page.headers['content-security-policy'] == web.static.CSP
        assert page.headers['content-disposition'].startswith('inline;')
        csv = client.get(url + 'records.csv?project_id=project')
        assert csv.status_code == 200 and csv.content == web.static.expected_output(RAW)
        assert csv.headers['content-disposition'].startswith('attachment;')
    # Frozen input and result survive an edit to the original material.
    files.files[0]['content'] = '[{"changed":true}]'
    assert runtime.snapshot('tenant', 'project', 'campaign', rid)['current_verification']['status'] == 'passed'
    runtime.advance('tenant', 'project', 'campaign', rid)
    assert files.writes == 3
    files.files[-1]['content'] = 'corrupted output'
    current = runtime.snapshot('tenant', 'project', 'campaign', rid)
    assert current['current_verification']['status'] == 'failed'
    assert current['deliverables'] == []


def test_partial_publication_readback_retains_frozen_input(release, monkeypatch):
    store, files = release
    rid = store.release['release_id']
    files.interrupt = True
    with pytest.raises(RuntimeError, match='lost response'):
        runtime.advance('tenant', 'project', 'campaign', rid)
    assert files.writes == 1 and store.stages[0]['status'] == 'passed'
    files.files[0]['content'] = '[{"later":true}]'
    def unavailable(*args):
        raise web.producer.WebToolUnavailable('Browser not installed')
    monkeypatch.setattr(web.producer, 'verify', unavailable)
    result = runtime.advance('tenant', 'project', 'campaign', rid)
    assert files.writes == 2
    assert [s['stage'] for s in result['stages'] if s['status'] == 'passed'] == list(runtime.STAGES[:3])
    assert result['stages'][-1]['status'] == 'unavailable'
    assert result['release']['status'] != 'finished'
    assert web.read(files.project_snapshot(), store.release, 'source.json')[0] == RAW


def test_wrong_download_never_passes_verification(release, monkeypatch):
    store, files = release
    monkeypatch.setattr(web.producer, 'verify', lambda *args: {
        'source_revision': store.release['contract']['selected_artifact']['sha256'],
        'output': {'content': 'wrong'}, 'observations': ['claimed success'], 'workflow': []})
    runtime.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert store.stages[-1]['status'] == 'failed'
    assert store.release['status'] != 'finished' and files.writes == 2


def test_revised_contract_uses_a_new_path(release):
    store, _ = release
    old = web.prefix(store.release)
    new = copy.deepcopy(store.release)
    new['contract_version'] = 2
    assert web.prefix(new) != old


@pytest.mark.parametrize('refs,raw', [(['input.json'], b'{"not":"records"}'),
    (['input.json', 'other.json'], RAW), (['bad.html'], b'<script>bad</script>')])
def test_unsupported_inputs_have_no_executable_recipe(refs, raw):
    assert web.compile_recipe({'files': [{'path': refs[0], 'content': raw.decode()}]}, refs) == (None, None)
