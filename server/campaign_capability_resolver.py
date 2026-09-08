"""Bounded capability discovery, distinct from proof of executable delivery."""
from copy import deepcopy

import deps
import catalog


def resolve(tenant, delivery_profile, *, existing_artifact=False, transform_recipe=False, cad_recipe=False):
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
                           'inputs': deepcopy(view['params']) if isinstance(view.get('params'), dict) else None,
                           'outputs': 'Unknown: no canonical output schema source established',
                           'readiness': 'unproven',
                           'permission_requirement': 'Current tenant and project authority',
                           'budget_constraint': 'Existing entitlement and quota checks required',
                           'verification_method': 'Actual invocation and output readback',
                           'missing_capability': 'Verified invocation adapter'})
    candidates.sort(key=lambda row: str(row['name']))
    selected = ({'cad_file': 'project_file_delivery', 'web_tool': 'managed_json_records_to_csv'}
                .get(delivery_profile) if existing_artifact else None)
    available = selected is not None
    native = ({'name': selected, 'operation': 'convert JSON records and download CSV',
               'inputs': 'One authorized project JSON file with flat records',
               'outputs': 'Managed HTML tool and verified CSV file', 'readiness': 'available',
               'permission_requirement': 'Current project write and read authority',
               'budget_constraint': '1 MiB files, 1000 records, 100 columns, one browser verifier',
               'verification_method': 'Actual Chromium conversion and downloaded byte comparison',
               'version': 1} if selected == 'managed_json_records_to_csv' else None)
    if transform_recipe:
        selected = 'published_json_records_to_csv'
        native = {'name': 'campaign-records-to-csv', 'operation': 'transform JSON records into CSV',
                  'inputs': {'source_json': 'UTF-8 flat JSON records, at most 1 MiB'},
                  'outputs': {'csv': 'Actual authored CSV, independently compared and retrieved'},
                  'readiness': 'unproven', 'version': 1,
                  'permission_requirement': 'Current project actor, published tool, run entitlement and execution policy',
                  'budget_constraint': 'Existing author quota and publication policy; at most three workspace jobs',
                  'verification_method': 'Exact published source in sandbox, actual job output and saved file readback'}
        return {'selected': selected, 'readiness': 'unproven', 'selected_capability': native,
                'shortlist': [c for c in candidates if c['name'] == 'campaign-records-to-csv'],
                'connected_mcp_tools': [], 'missing_capability': 'Verified published CSV invocation',
                'recommended_action': 'Reuse the published CSV tool or acquire it through existing author authority',
                'blocks_dispatch': False}
    if cad_recipe and delivery_profile == 'cad_file' and existing_artifact:
        selected = 'managed_dxf_layer_inventory'
        available = True
        native = {'name': selected, 'recipe_id': 'dxf-layer-summary', 'version': 1,
                  'operation': 'Summarize direct supported ASCII DXF entities by layer',
                  'inputs': 'One validated ASCII DXF in the authorized project',
                  'outputs': 'Deterministic layer summary CSV and a frozen source copy',
                  'readiness': 'available',
                  'permission_requirement': 'Current tenant identity and project write/read authority',
                  'budget_constraint': 'Existing project limits; at most 1 MiB input and output; no external services',
                  'verification_method': 'Parse frozen DXF, compute layer counts, validate and retrieve receipt-backed CSV bytes'}
    return {'selected': selected,
            'readiness': 'available' if available else 'unavailable',
            'selected_capability': native,
            'shortlist': candidates[:8], 'connected_mcp_tools': [],
            'missing_capability': None if available else (
                'One valid JSON records file for the managed converter, or another verified web adapter' if delivery_profile == 'web_tool'
                else 'Validated project artifact and executable delivery adapter'),
            'connected_mcp_limitation': 'No connected MCP invocation adapter',
            'recommended_action': 'Build the missing output through the existing campaign executor',
            'blocks_dispatch': False}


def acquisition_dependency():
    return {'readiness': 'unproven', 'change_set_id': None,
            'missing_capability': 'Authorized customization session/turn integration',
            'recommended_action': 'Use the existing authorized customization entry point'}
