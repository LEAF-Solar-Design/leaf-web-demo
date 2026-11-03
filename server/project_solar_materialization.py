"""Publish an authoritative physical layout through canonical drawing custody.

The native metadata writer is an explicit boundary. The geometry-only mutation
Activity and intake DXF synthesizer cannot implement that writer. Its absence
must never turn into a successful polyline-only layout publication.
"""
from copy import deepcopy
import json
from uuid import UUID, uuid5

import project_drawing_editor as editor
import solar_physical_head as physical
import solar_physical_state as state
from solar_design_graph import validate_graph, GraphValidationError
from solar_solve_results import digest

project = editor.project
SCHEMA = 'leaf.project-physical-layout.v1'
KEY = 'solar_physical_layout'


def initial_parameters(body):
    if type(body) is not dict or set(body) != {'boundary', 'preset', 'drawing_units'}:
        fail('REQUEST_INVALID', 400)
    return deepcopy(body)


def _initial_scope(tenant, project_id):
    org, actor = project._access(tenant, project_id, write=True)
    project.platform_link.platform_db().run_transaction(lambda conn:
        project.graph_store().initial_publication_scope(org, project_id, actor, conn=conn))
    return org, actor


def initial_layout_source(tenant, project_id):
    org, _ = _initial_scope(tenant, project_id)
    return {'schema': SCHEMA, 'mode': 'initial', 'organization_id': str(org),
            'project_id': str(project_id), 'drawing_id': None, 'parent_version_id': None,
            'writer_available': False, 'reason_code': 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE'}


def _initial_receipt(version, request_id, metadata):
    proof = version.provenance
    return {'drawing_version': version.to_dict(), 'organization_id': str(version.org_id),
            'project_id': str(version.project_id), 'drawing_id': str(version.drawing_id),
            'parent_version_id': None, 'request_id': str(request_id), 'mode': 'initial',
            'source_sha256': proof['source']['sha256'], 'intake_sha256': proof['intake']['sha256'],
            'generation_source': deepcopy(metadata['generation_source'])}


def publish_initial_layout(tenant, project_id, body, *, request_id):
    """Generate from parameters and publish the first real canonical artifact atomically."""
    import solar_frames_piles as fp
    body = initial_parameters(body)
    org, actor = project._access(tenant, project_id, write=True)
    if not isinstance(request_id, UUID):
        project.refuse('INVALID_BINDING')
    database, graph = project.platform_link.platform_db(), project.graph_store()
    def lookup(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT version_id FROM drawing_versions WHERE org_id=%s AND project_id=%s '
                        'AND idempotency_key=%s', (org, project_id, 'sip-r2:' + str(request_id)))
            return cur.fetchone()
    prior = database.run_transaction(lookup)
    if prior is not None:
        context = project.resolve_context(tenant, project_id, prior['version_id'], write=True)
        metadata, proof = context.intake.get(KEY) or {}, context.binding.version.provenance
        if (proof.get('schema') != graph.INITIAL_PUBLICATION_SCHEMA
                or proof.get('actor_binding_id') != str(actor) or metadata.get('request') != body
                or proof.get('parameters_sha256') != digest(body)):
            raise project.ProjectContextError('SIP_R2_IDEMPOTENCY_CONFLICT')
        editor.read_dxf(tenant, project_id, context.binding.drawing_id, prior['version_id'])
        return _initial_receipt(context.binding.version, request_id, metadata)
    _initial_scope(tenant, project_id)
    if project.write_loop.drawing_mutations_refusal() is not None:
        project.refuse('WRITES_DRAINED')
    try:
        document, inputs = fp.prepare_initial_frames(**body)
    except (fp.FramesPilesError, state.PhysicalStateError) as exc:
        raise editor.EditorError(exc.code, 422) from None
    drawing_id = uuid5(project_id, 'solar-initial:' + str(request_id))
    generation_source = {'parameters_sha256': digest(body),
                         'physical_content_sha256': editor._sha(state.encode_document(document))}
    # No imported source, synthetic base drawing, or canonical row exists here.
    output = materialize_native_layout({'organization_id': org, 'project_id': project_id,
        'actor_binding_id': actor, 'drawing_id': drawing_id}, None, deepcopy(document), None,
        parameters=deepcopy(inputs))
    intake = json.loads(editor._parse(output, drawing_id))
    metadata = {'schema': SCHEMA, 'mode': 'initial', 'parent_version_id': None,
                'request': body, 'generation_source': generation_source,
                'document': state.encode_document(document).decode('utf-8'), 'parameters': inputs}
    intake[KEY] = metadata
    payload = json.dumps(intake, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    source_sha, intake_sha = editor._sha(output), editor._sha(payload)
    backend = editor._backend(org)
    source_key = graph.publication_source_key(org, project_id, drawing_id, source_sha)
    intake_key = graph.publication_intake_key(org, project_id, drawing_id, intake_sha)
    def operation(conn):
        def durable():
            try:
                backend.put_if_absent_or_verify(source_key, output)
                backend.put_if_absent_or_verify(intake_key, payload)
            except Exception:
                fail('SOURCE_UNAVAILABLE', 503)
            editor._verified_bytes(backend, source_key, source_sha)
            project._load_intake(org, intake_key, intake_sha)
        version = graph.publish_initial_version(org, project_id, drawing_id,
            actor_binding_id=actor, request_id=request_id, intake_sha256=intake_sha,
            source_sha256=source_sha, parameters_sha256=digest(body), conn=conn, before_new=durable)
        return _initial_receipt(version, request_id, metadata)
    try:
        with project.write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                project.refuse('WRITES_DRAINED')
            return database.run_transaction(operation)
    except (editor.EditorError, project.ProjectContextError):
        raise
    except Exception:
        project.refuse('STORE_UNAVAILABLE')


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
