import campaign_capability_resolver as resolver


def test_catalog_registration_is_not_runtime_proof(monkeypatch):
    tools = [({'name': 'web-builder', 'source_revision': 'abc'}, 'tenant_override'),
             ({'name': '_private', 'internal': True}, 'operator_owned_engine')]
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance', lambda tenant: tools)
    result = resolver.resolve('tenant', 'web_tool')
    assert result['readiness'] == 'unavailable'
    assert result['connected_mcp_tools'] == []
    assert result['shortlist'][0]['catalog_digest'].startswith('sha256:')
    assert result['shortlist'][0]['source_revision'] == 'abc'
    assert result['shortlist'][0]['readiness'] == 'unproven'
    assert len(result['shortlist']) == 1


def test_shortlist_is_bounded_and_local_file_route_explicit(monkeypatch):
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance',
                        lambda tenant: [({'name': 'tool-' + str(i)}, 'tenant_override') for i in range(20)])
    result = resolver.resolve('tenant', 'cad_file', existing_artifact=True)
    assert len(result['shortlist']) == 8
    assert result['selected'] == 'project_file_delivery'
    assert resolver.acquisition_dependency()['readiness'] == 'unproven'
