"""Fixed Native profile projection over the shared project source transaction."""
from __future__ import annotations
import importlib

import browser_project_authoring as authoring
import browser_project_flow as flow

SOURCE_PROFILE = authoring.NATIVE_PROFILE
FlowError = flow.FlowError
MAX_INPUT_BYTES = flow.MAX_INPUT_BYTES
MAX_RESPONSE_BYTES = flow.MAX_RESPONSE_BYTES
_ERRORS = flow._ERRORS
canonical = flow.canonical
scope = flow.scope
inspection = flow.inspection


def author(tenant, project_id, request, *, cancelled=None):
    _require_video_prerequisites(tenant, project_id, request, cancelled=cancelled)
    return authoring.author(tenant, project_id, request, cancelled=cancelled,
                            source_profile=SOURCE_PROFILE)


def _require_video_prerequisites(tenant, project_id, request, *, cancelled=None):
    """Only this local pre-author phase can prove generation has not begun."""
    request = authoring._request(request, source_profile=SOURCE_PROFILE)
    if request['source_operation'] != 'create':
        return
    current = authoring._scope(tenant, project_id)
    def recheck():
        if cancelled is not None and cancelled():
            raise authoring.AuthoringError('cancelled')
        if authoring._scope(tenant, project_id) != current:
            raise authoring.AuthoringError('access_denied')
    authoring.platform_link._ensure_platform_package()
    from leaf_platform import project_reference_materials as metadata
    for ref in request['material_refs']:
        recheck()
        try:
            row = metadata.get_material(*current, ref['material_id'])
        except Exception:
            raise authoring.AuthoringError('unavailable') from None
        recheck()
        if (not isinstance(row, dict) or row.get('material_id') != ref['material_id']
                or (row.get('org_id'), row.get('project_id'), row.get('actor_binding_id')) != current
                or row.get('sha256') != ref['sha256']):
            raise authoring.AuthoringError('source_changed')
        if row.get('media_type') != 'video/quicktime':
            continue
        try:
            decoder = importlib.import_module('project_video_materials')
        except ModuleNotFoundError as exc:
            if exc.name != 'project_video_materials':
                raise
            available = False
        else:
            available = decoder._available()
        recheck()
        if not available:
            raise flow.VideoUnavailableBeforeGeneration()


def parse_request(raw, action):
    return flow.parse_request(raw, action, source_profile=SOURCE_PROFILE)


def read(tenant, project_id, edit_id=None):
    return flow.read(tenant, project_id, edit_id, source_profile=SOURCE_PROFILE)


def mutate(tenant, project_id, session_id, turn_id, action, edit_id, body):
    return flow.mutate(tenant, project_id, session_id, turn_id, action, edit_id, body,
                       source_profile=SOURCE_PROFILE)
