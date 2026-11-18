"""Joined Native route, CAS flow, initializer failure and private middleware."""
import json
import uuid
import pytest
import browser_project_authoring as authoring
import project_author_diagnostics as diagnostics
from test_native_project_routes import lane, connected, create, EDIT


@pytest.mark.parametrize('subphase,http_status', [('initialize_http', 503), ('initialize_transport', None)])
def test_initializer_failure_keeps_correlated_public_error_without_partial_candidate(
        connected, monkeypatch, subphase, http_status):
    def unavailable(*args, **kwargs):
        diagnostics.record_failure(subphase, http_status=http_status)
        raise authoring.source.SourceUnavailable('PRIVATE initializer detail')
    monkeypatch.setattr(authoring.source, 'initialize_project_source', unavailable)
    response = create(connected)
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'private, no-store'
    public = response.json()['error']
    event = [event['data'] for event in connected.events if event['type'] == 'browser_source_error'][0]
    assert public == dict(code='author_unavailable',
        message='Project source initialization failed. Review the original request before retrying.',
        source_request_id=EDIT, correlation_id=event['correlation_id'], phase='stage', author_subphase=subphase,
        **({'author_http_status': http_status} if http_status else {}))
    assert uuid.UUID(public['correlation_id']).version == 4
    assert not connected.posts and not connected.candidates and not connected.confirmations
    assert connected.active is None and diagnostics.snapshot() == {}
    assert 'PRIVATE' not in json.dumps([public, event])
