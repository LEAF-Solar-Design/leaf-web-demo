"""Publish an authoritative physical layout through canonical drawing custody.

The native metadata writer is an explicit boundary. The geometry-only mutation
Activity and intake DXF synthesizer cannot implement that writer. Its absence
must never turn into a successful polyline-only layout publication.
"""
from copy import deepcopy
import json
from uuid import UUID

import project_drawing_editor as editor
import solar_physical_head as physical
import solar_physical_state as state
from solar_design_graph import validate_graph, GraphValidationError
from solar_solve_results import digest

project = editor.project
SCHEMA = 'leaf.project-physical-layout.v1'
KEY = 'solar_physical_layout'


def fail(code, status=409):
    raise editor.EditorError('PROJECT_LAYOUT_' + code, status, status == 503)


def parameters(body):
    if (type(body) is not dict or set(body) != {'physical_head', 'physical_content_sha256', 'physical_index'}
            or not editor._digest(body['physical_head']) or not editor._digest(body['physical_content_sha256'])
            or type(body['physical_index']) is not int or not 0 <= body['physical_index'] < physical.MAX_LOG_ENTRIES):
        fail('REQUEST_INVALID', 400)
    return deepcopy(body)


def _source(context):
    """Follow only this artifact's verified canonical ancestry to its account source."""
    current, seen = context, set()
    for _ in range(128):
        binding = current.binding
        if binding.input_version_id in seen:
            fail('SOURCE_INVALID')
        seen.add(binding.input_version_id)
        proof = binding.version.provenance
        if proof.get('schema') == 'leaf.drawing-import.v1':
            source = proof.get('source') or {}
            # The existing editor checks account-upload source identity and org.
            editor._source(current)
            return source['drawing_id'], source['version']
        try:
            parent = project.graph_store().resolve_version_binding(binding.organization_id,
                binding.project_id, UUID(proof['parent_version_id']), drawing_id=binding.drawing_id)
            current = project._read_intake(parent)
        except project.ProjectContextError:
            raise
        except Exception:
            fail('SOURCE_INVALID')
    fail('SOURCE_INVALID')


def _physical_context(context):
    intake = context.intake
    if 'solar_design_graph' not in intake:
        fail('GRAPH_REQUIRED')
    try:
        graph = validate_graph(intake['solar_design_graph'])
        if digest(graph) != intake.get('solar_design_graph_sha256'):
            fail('GRAPH_INVALID')
    except GraphValidationError:
        fail('GRAPH_INVALID')
    binding = context.binding
    drawing, version = _source(context)
    backend = editor._backend(binding.organization_id)
    return binding, graph, drawing, version, backend


def current_layout_source(tenant, project_id, drawing_id, parent_id):
    context = project.resolve_context(tenant, project_id, parent_id, drawing_id=drawing_id)
    if not context.binding.is_head:
        project.refuse('STALE_VERSION')
    binding, graph, drawing, _, backend = _physical_context(context)
    try:
        head, _ = physical.load_physical_head(backend, str(binding.organization_id), drawing,
                                              project_id=graph['project']['id'])
        if head is None:
            fail('PHYSICAL_HEAD_REQUIRED')
        body = {'physical_head': head['state']['artifact_id'],
                'physical_content_sha256': head['state']['content_sha256'], 'physical_index': head['index']}
        _load(context, body)
    except (physical.PhysicalHeadError, state.PhysicalStateError):
        fail('PHYSICAL_SOURCE_INVALID')
    return {'schema': SCHEMA, 'organization_id': str(binding.organization_id),
            'project_id': str(project_id), 'drawing_id': str(drawing_id), 'parent_version_id': str(parent_id),
            'physical_source': body, 'writer_available': False,
            'reason_code': 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE'}


def _load(context, body):
    binding, graph, drawing, version, backend = _physical_context(context)
    try:
        head, document = physical.load_physical_head(backend, str(binding.organization_id), drawing,
                                                     project_id=graph['project']['id'])
        if head is None:
            fail('PHYSICAL_HEAD_REQUIRED')
        meta, stored = state.load_physical_state(backend, str(binding.organization_id), drawing,
            body['physical_head'], project_id=graph['project']['id'])
        encoded = state.encode_document(document)
        if (head['drawing_id'] != drawing or head['project_id'] != graph['project']['id']
                or head['index'] != body['physical_index'] or head['state']['artifact_id'] != body['physical_head']
                or head['state']['content_sha256'] != body['physical_content_sha256']
                or editor._sha(encoded) != body['physical_content_sha256']
                or state.encode_document(stored) != encoded or meta['source_version'] != version
                or meta['graph_sha256'] != digest(graph)):
            fail('PHYSICAL_HEAD_STALE')
        inputs = state.load_operation_parameters(backend, str(binding.organization_id), drawing, meta, document)
        if type(inputs.get('generation')) is not dict:
            fail('PARAMETERS_REQUIRED')
    except editor.EditorError:
        raise
    except (physical.PhysicalHeadError, state.PhysicalStateError):
        fail('PHYSICAL_SOURCE_INVALID')
    except GraphValidationError as exc:
        fail('PARAMETERS_REQUIRED' if exc.code == 'ARTIFACT_NOT_FOUND' else 'PHYSICAL_SOURCE_INVALID')
    return head, document, inputs


def materialize_native_layout(context, head, document, source, *, parameters):
    """Required native physical-layout writer, deliberately not a geometry fallback.

    solar_apply_plan builds native store/carrier plans from captured adapters;
    write_loop's generic mutation Activity cannot write this neutral physical
    document's complete native metadata. No existing executor accepts that input.
    A real licensed implementation must replace this server-owned boundary and
    independently verify its returned native bytes before enabling this route.
    """
    fail('LICENSED_WRITER_UNAVAILABLE', 503)


def _replay(tenant, org, proj, drawing, parent, request, body, actor, capability):
    def lookup(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT version_id FROM drawing_versions WHERE org_id=%s AND project_id=%s '
                        'AND idempotency_key=%s', (org, proj, 'sip-r2:' + str(request)))
            return cur.fetchone()
    row = project.platform_link.platform_db().run_transaction(lookup)
    if row is None:
        return None
    context = project.resolve_context(tenant, proj, row['version_id'], drawing_id=drawing, write=True)
    proof = context.binding.version.provenance
    metadata = context.intake.get(KEY) or {}
    if (metadata.get('schema') != SCHEMA or metadata.get('request') != body
            or metadata.get('parent_version_id') != str(parent)
            or proof.get('actor_binding_id') != str(actor)):
        raise project.ProjectContextError('SIP_R2_IDEMPOTENCY_CONFLICT')
    raw, _ = editor.read_dxf(tenant, proj, drawing, row['version_id'])
    return editor.save_dxf(tenant, proj, drawing, parent, raw, request_id=request,
                          capability=capability, _restored_intake=context.intake_bytes)


def publish_layout(tenant, project_id, drawing_id, parent_id, body, *, request_id, capability):
    body = parameters(body)
    org, actor = project._access(tenant, project_id, write=True)
    if not all(isinstance(value, UUID) for value in (drawing_id, parent_id, request_id)):
        project.refuse('INVALID_BINDING')
    replay = _replay(tenant, org, project_id, drawing_id, parent_id, request_id, body, actor, capability)
    if replay is not None:
        return {**replay, 'physical_source': deepcopy(body)}
    admission = project.verify_at_admission(tenant, project_id, drawing_id, parent_id,
                                          checkout_capability=capability)
    context = admission.context
    head, document, inputs = _load(context, body)
    raw, _ = editor.read_dxf(tenant, project_id, drawing_id, parent_id)
    # Only a trusted server-owned writer receives the complete authoritative
    # document. Request data contains no geometry or replacement metadata.
    output = materialize_native_layout(context, deepcopy(head), deepcopy(document), raw, parameters=deepcopy(inputs))
    parsed = editor._parse(output, drawing_id)
    intake = json.loads(editor._edited_intake(context, parsed))
    intake[KEY] = {'schema': SCHEMA, 'parent_version_id': str(parent_id), 'request': body,
                   'head': head, 'document': state.encode_document(document).decode('utf-8'), 'parameters': inputs}
    payload = json.dumps(intake, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    # Recheck authority and source after the external writer returns. A late
    # answer must not replace another selected version or a newer physical head.
    project.verify_at_admission(tenant, project_id, drawing_id, parent_id, checkout_capability=capability)
    latest, _, _ = _load(context, body)
    if latest != head:
        fail('PHYSICAL_HEAD_STALE')
    receipt = editor.save_dxf(tenant, project_id, drawing_id, parent_id, output,
                            request_id=request_id, capability=capability, _restored_intake=payload)
    return {**receipt, 'physical_source': deepcopy(body)}
