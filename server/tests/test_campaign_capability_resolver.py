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


def test_transform_selection_requires_actual_invocation_proof(monkeypatch):
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance', lambda tenant: [])
    result = resolver.resolve('tenant', 'cad_file', existing_artifact=True, transform_recipe=True)
    assert result['selected'] == 'published_json_records_to_csv'
    assert result['readiness'] == 'unproven'
    assert result['selected_capability']['outputs']['csv'].startswith('Actual authored CSV')
    assert result['missing_capability'] and result['shortlist'] == []


def test_candidate_contract_copies_canonical_params_without_execution_metadata(monkeypatch):
    params = {'type': 'object',
              'properties': {'records': {'type': 'array', 'items': {'type': 'object',
                             'properties': {'name': {'type': 'string'}}}}},
              'required': ['records'], 'additionalProperties': False}
    tool = {'name': 'records-tool', 'params': params,
            'credentials': {'token': 'private-token'}, 'source': 'private-source',
            'execution_policy': {'privileged': True},
            'outputs': {'type': 'object'}, 'output_schema': {'type': 'string'}}
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance',
                        lambda tenant: [(tool, 'tenant_override')])
    candidate = resolver.resolve('tenant', 'web_tool')['shortlist'][0]
    assert candidate['inputs'] == params
    assert candidate['inputs'] is not params
    assert candidate['outputs'] == 'Unknown: no canonical output schema source established'
    assert not {'credentials', 'source', 'execution_policy', 'output_schema'} & candidate.keys()
    assert 'private-token' not in str(candidate)
    assert 'private-source' not in str(candidate)
    candidate['inputs']['properties']['records']['items']['properties']['name']['type'] = 'number'
    candidate['inputs']['required'].append('other')
    assert params['properties']['records']['items']['properties']['name']['type'] == 'string'
    assert params['required'] == ['records']
    fresh = resolver.resolve('tenant', 'web_tool')['shortlist'][0]
    assert fresh['inputs'] == params
    params['properties']['records']['type'] = 'string'
    assert fresh['inputs']['properties']['records']['type'] == 'array'


def test_candidate_missing_or_non_object_params_are_unknown(monkeypatch):
    for fields in ({}, {'params': None}, {'params': 'placeholder'}, {'params': []}):
        monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance',
                            lambda tenant: [({'name': 'tool', **fields}, 'tenant_override')])
        candidate = resolver.resolve('tenant', 'web_tool')['shortlist'][0]
        assert candidate['inputs'] is None
        assert candidate['outputs'] == 'Unknown: no canonical output schema source established'
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance',
                        lambda tenant: [({'name': 'tool', 'params': {}}, 'tenant_override')])
    assert resolver.resolve('tenant', 'web_tool')['shortlist'][0]['inputs'] == {}


def test_schema_presence_does_not_grant_readiness_or_execution_authority(monkeypatch):
    tool = {'name': 'campaign-records-to-csv', 'params': {'type': 'object'},
            'readiness': 'available', 'permission_requirement': 'None',
            'budget_constraint': 'Unlimited'}
    monkeypatch.setattr(resolver.deps, 'effective_tools_with_provenance',
                        lambda tenant: [(tool, 'tenant_override')])
    for transform_recipe in (False, True):
        result = resolver.resolve('tenant', 'web_tool', transform_recipe=transform_recipe)
        assert result['readiness'] == ('unproven' if transform_recipe else 'unavailable')
        assert result['connected_mcp_tools'] == []
        assert result['blocks_dispatch'] is False
        candidate = result['shortlist'][0]
        assert candidate['inputs'] == tool['params']
        assert candidate['readiness'] == 'unproven'
        assert candidate['permission_requirement'] == 'Current tenant and project authority'
        assert candidate['budget_constraint'] == 'Existing entitlement and quota checks required'
        assert candidate['missing_capability'] == 'Verified invocation adapter'
