"""Fixed Native profile projection over the shared project source transaction."""
from __future__ import annotations

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
    return authoring.author(tenant, project_id, request, cancelled=cancelled,
                            source_profile=SOURCE_PROFILE)


def parse_request(raw, action):
    return flow.parse_request(raw, action, source_profile=SOURCE_PROFILE)


def read(tenant, project_id, edit_id=None):
    return flow.read(tenant, project_id, edit_id, source_profile=SOURCE_PROFILE)


def mutate(tenant, project_id, session_id, turn_id, action, edit_id, body):
    return flow.mutate(tenant, project_id, session_id, turn_id, action, edit_id, body,
                       source_profile=SOURCE_PROFILE)
