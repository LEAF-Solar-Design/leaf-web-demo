"""Publish complete native physical layouts through canonical drawing custody."""
from copy import deepcopy
from dataclasses import asdict
import json
from uuid import UUID, uuid5

import project_drawing_editor as editor
import solar_physical_head as physical
import solar_physical_state as state
import solar_native_layout_dxf as native_layout
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
            **serialization_availability()}


def serialization_availability():
    return {'writer_available': True, 'serialization_available': True,
            'native_execution_available': False, 'autocad_acceptance': False}


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
    _generation_provenance(inputs, document, body['preset'])
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
    if KEY in context.intake:
        head, _, _ = _canonical_state(context)
        return {'schema': SCHEMA, 'organization_id': str(context.binding.organization_id),
                'project_id': str(project_id), 'drawing_id': str(drawing_id), 'parent_version_id': str(parent_id),
                'physical_source_kind': 'canonical_version_intake', 'physical_version_id': str(parent_id),
                'physical_source': _head_request(head), 'operations': ['frame-generate', 'piling-generate'],
                **serialization_availability()}
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
            'physical_source': body, **serialization_availability()}


def _load(context, body):
    if KEY in context.intake:
        head, document, inputs = _canonical_state(context)
        if _head_request(head) != body:
            fail('PHYSICAL_HEAD_STALE')
        return head, document, inputs
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


def _head_request(head):
    return {'physical_head': head['state']['artifact_id'],
            'physical_content_sha256': head['state']['content_sha256'], 'physical_index': head['index']}


def _canonical_state(context, *, conn=None):
    """Read physical state from verified canonical bytes, never an invented legacy binding."""
    binding, metadata = context.binding, context.intake.get(KEY)
    if type(metadata) is not dict or metadata.get('schema') != SCHEMA:
        fail('NATIVE_METADATA_INVALID')
    source_context, seen = context, set()
    while (source_context.binding.version.provenance.get('schema') == 'leaf.project-drawing-publication.v1'
           and source_context.binding.version.provenance.get('source') is None):
        source_binding = source_context.binding
        if source_binding.input_version_id in seen or len(seen) >= 128:
            fail('SOURCE_INVALID')
        seen.add(source_binding.input_version_id)
        parent = project.graph_store().resolve_version_binding(source_binding.organization_id,
            source_binding.project_id, UUID(source_binding.version.provenance['parent_version_id']),
            drawing_id=source_binding.drawing_id, conn=conn)
        if parent.version.oss_object != binding.version.oss_object:
            fail('SOURCE_INVALID')
        source_context = project._read_intake(parent)
    key, source_sha, _ = editor._source(source_context)
    raw = editor._verified_bytes(editor._backend(binding.organization_id), key, source_sha)
    try:
        decoded = native_layout.decode_native_layout(raw)
        encoded = state.encode_document(decoded.document)
    except (native_layout.NativeLayoutError, state.PhysicalStateError):
        fail('NATIVE_METADATA_INVALID')
    expected_scope = {'organization_id': str(binding.organization_id), 'project_id': str(binding.project_id),
                      'drawing_id': str(binding.drawing_id)}
    if decoded.scope != expected_scope:
        fail('NATIVE_SCOPE_MISMATCH')
    if (metadata.get('document') != encoded.decode('utf-8')
            or type(metadata.get('parameters')) is not dict
            or digest(metadata['parameters']) != digest(decoded.parameters)
            or decoded.parameters.get('schema') != state.PARAMETERS_SCHEMA
            or type(decoded.parameters.get('generation')) is not dict):
        fail('NATIVE_METADATA_INVALID')
    head = {'kind': 'canonical_version_intake', 'version_id': str(binding.input_version_id),
            'drawing_id': str(binding.drawing_id), 'project_id': str(binding.project_id),
            'index': binding.version.seq - 1,
            'state': {'artifact_id': context.intake_sha256, 'content_sha256': editor._sha(encoded)}}
    return head, decoded.document, decoded.parameters


def _generation_provenance(inputs, document, preset, *, existing=()):
    """Keep the actual parsed model, including order-sensitive native pack semantics."""
    import solar_frames_piles as fp
    loaded, _ = fp.load_preset(preset)
    models = inputs.setdefault('generation_models', [])
    associations = inputs.setdefault('frame_generations', {})
    index = len(models)
    models.append(asdict(loaded))
    for frame in document['state']['frames']:
        if frame['handle'] not in existing:
            associations[frame['handle']] = index


def canonical_ground_input(context, *, conn=None):
    """Reconstruct trusted physical input from this exact stored graph parent."""
    binding = project.graph_store().resolve_version_binding(context.organization_id, context.project_id,
        context.parent_version_id, drawing_id=context.drawing_id, conn=conn)
    verified = project._read_intake(binding)
    if verified.intake_bytes != context.intake_bytes or verified.intake_sha256 != context.intake_sha256:
        fail('NATIVE_METADATA_INVALID')
    head, document, inputs = _canonical_state(verified, conn=conn)
    head['parameters'] = inputs
    return {'view': head, 'document': document}


def operation_parameters(body):
    if (type(body) is not dict or set(body) != {'physical_source', 'operation', 'parameters'}
            or body['operation'] not in ('frame-generate', 'piling-generate')
            or type(body['parameters']) is not dict):
        fail('REQUEST_INVALID', 400)
    parameters(body['physical_source'])
    try:
        digest(body)
    except (ValueError, TypeError, RecursionError):
        fail('REQUEST_INVALID', 400)
    return deepcopy(body)


def publish_operation(tenant, project_id, drawing_id, parent_id, body, *, request_id, capability):
    import solar_frames_piles as fp
    body = operation_parameters(body)
    org, actor = project._access(tenant, project_id, write=True)
    if not all(isinstance(value, UUID) for value in (drawing_id, parent_id, request_id)):
        project.refuse('INVALID_BINDING')
    replay = _replay(tenant, org, project_id, drawing_id, parent_id, request_id, body, actor, capability)
    if replay is None:
        admission = project.verify_at_admission(tenant, project_id, drawing_id, parent_id,
                                              checkout_capability=capability)
        context = admission.context
        if KEY not in context.intake:
            fail('NATIVE_METADATA_REQUIRED')
        head, document, inputs = _load(context, body['physical_source'])
        existing_handles = {frame['handle'] for frame in document['state'].get('frames', [])}
        try:
            document, inputs = fp.continue_physical_document(document, inputs,
                                                             body['operation'], body['parameters'])
        except (fp.FramesPilesError, state.PhysicalStateError) as exc:
            raise editor.EditorError(exc.code, 422) from None
        if body['operation'] == 'frame-generate':
            _generation_provenance(inputs, document, body['parameters']['preset'], existing=existing_handles)
        raw, _ = editor.read_dxf(tenant, project_id, drawing_id, parent_id)
        output = materialize_native_layout(context, head, document, raw, parameters=inputs)
        intake = json.loads(editor._edited_intake(context, editor._parse(output, drawing_id)))
        intake[KEY] = {'schema': SCHEMA, 'mode': 'canonical-operation', 'parent_version_id': str(parent_id),
                       'request': body, 'head': head, 'document': state.encode_document(document).decode(),
                       'parameters': inputs}
        payload = json.dumps(intake, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        project.verify_at_admission(tenant, project_id, drawing_id, parent_id, checkout_capability=capability)
        _load(context, body['physical_source'])
        replay = editor.save_dxf(tenant, project_id, drawing_id, parent_id, output,
                                request_id=request_id, capability=capability, _restored_intake=payload)
    return {**replay, 'physical_source': body['physical_source'], 'operation': body['operation']}


def materialize_native_layout(context, head, document, source, *, parameters):
    """Serialize native tags and complete scoped metadata without executing CAD."""
    try:
        return native_layout.materialize_native_layout(context, head, document, source,
                                                       parameters=parameters)
    except native_layout.NativeLayoutError as exc:
        raise editor.EditorError(exc.code, 422) from None


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
    if (metadata.get('schema') != SCHEMA or digest(metadata.get('request')) != digest(body)
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
