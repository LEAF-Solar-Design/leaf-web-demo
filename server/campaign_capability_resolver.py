"""Bounded capability discovery, distinct from proof of executable delivery."""
import deps
import catalog


def resolve(tenant, delivery_profile, *, existing_artifact=False):
    tenant_id = str(getattr(tenant, 'tenant_id', tenant))
    candidates = []
    for tool, provenance in deps.effective_tools_with_provenance(tenant_id):
        if not catalog.filter_internal([tool]):
            continue
        view = deps.catalog_tool_view(tool)
        candidates.append({'name': view.get('name'), 'provenance': provenance,
                           'catalog_digest': view['catalog_digest'],
                           'source_revision': view.get('source_revision', view.get('catalog_commit')),
                           'operation': view.get('name'),
                           'inputs': 'Published tool input schema; invocation adapter must validate it',
                           'outputs': 'Published tool output schema; actual output remains unverified',
                           'readiness': 'unproven',
                           'permission_requirement': 'Current tenant and project authority',
                           'budget_constraint': 'Existing entitlement and quota checks required',
                           'verification_method': 'Actual invocation and output readback',
                           'missing_capability': 'Verified invocation adapter'})
    candidates.sort(key=lambda row: str(row['name']))
    available = delivery_profile == 'cad_file' and existing_artifact
    return {'selected': 'project_file_delivery' if available else None,
            'readiness': 'available' if available else 'unavailable',
            'shortlist': candidates[:8], 'connected_mcp_tools': [],
            'missing_capability': None if available else (
                'Executable website deployment adapter' if delivery_profile == 'web_tool'
                else 'Validated project artifact and executable delivery adapter'),
            'connected_mcp_limitation': 'No connected MCP invocation adapter',
            'recommended_action': 'Build the missing output through the existing campaign executor',
            'blocks_dispatch': False}


def acquisition_dependency():
    return {'readiness': 'unproven', 'change_set_id': None,
            'missing_capability': 'Authorized customization session/turn integration',
            'recommended_action': 'Use the existing authorized customization entry point'}
