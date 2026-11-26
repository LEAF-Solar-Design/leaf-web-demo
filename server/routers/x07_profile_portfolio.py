"""Read profile reuse decisions and placement previews for authorized drawing revisions."""
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

import deps
import platform_link
import solar_project_context as context_service
from routers.project_drawings import _dispatch, _headers
from routers.x07_profile_history import _fields
from routers.x07_profiles import PATH as ROLE_PROFILE_PATH
from routers.x07_profiles import PlacementOverride, PlacementWindow
from solar_design_graph import GraphValidationError
from x07_panel_group_review import compare_panel_groups
from x07_customer_profile_store import CustomerProfileStore, MAX_PROFILE_VERSION
from x07_postgres_profile_store import PostgresProfileStore
from x07_profile_compatibility import check_compatibility
from x07_role_selection import (
    HANDLE, SelectionError, compare_placement_override, select_modules,
)

router = APIRouter()
PORTFOLIO_PATH = '/api/role-profiles/reuse-review'
REMOVED_ROLES_EXPORT_PATH = PORTFOLIO_PATH + '/export'
PLACEMENT_OVERRIDE_PREVIEW_PATH = ROLE_PROFILE_PATH + '/placement-overrides/preview'
PLACEMENT_CHANGE_PREVIEW_PATH = ROLE_PROFILE_PATH + '/placement-changes/preview'
PANEL_GROUP_CHANGE_PREVIEW_PATH = ROLE_PROFILE_PATH + '/panel-group-changes/preview'
MAX_PREVIEW_WINDOWS = 4
MAX_REPORTED_CHANGES = 256
_UNAVAILABLE = {
    'SIP_R1_INTAKE_PROOF_REQUIRED',
    'SIP_R1_INTAKE_REFERENCE_INVALID',
    'SIP_R1_INTAKE_DIGEST_MISMATCH',
    'SIP_R1_INTAKE_INVALID',
    'SIP_R1_INTAKE_UNAVAILABLE',
}


class DrawingRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    project_id: UUID
    drawing_id: UUID
    input_version_id: UUID


class ReuseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revisions: list[DrawingRevision] = Field(min_length=1, max_length=16)

    @field_validator('revisions')
    @classmethod
    def unique_revisions(cls, revisions):
        if len(set(revisions)) != len(revisions):
            raise ValueError('duplicate drawing revision')
        return revisions


class RemovedRolesExportRequest(ReuseRequest):
    profile_id: UUID
    through_version: int = Field(strict=True, ge=1, le=MAX_PROFILE_VERSION)


class PlacementRole(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block: StrictStr = Field(min_length=1, max_length=512)
    layer: StrictStr = Field(min_length=1, max_length=512)


class PanelGroupChangeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    previous_input_version_id: UUID


class PlacementPreviewRequest(PlacementRole):
    previous_input_version_id: UUID


class PlacementOverridePreviewRequest(PlacementPreviewRequest):
    overrides: list[PlacementOverride] = Field(min_length=1, max_length=MAX_PREVIEW_WINDOWS)


class PlacementChangePreviewRequest(PlacementPreviewRequest):
    previous_role: PlacementRole | None = None
    windows: list[PlacementWindow] = Field(min_length=1, max_length=MAX_PREVIEW_WINDOWS)


def _authorized_customer(tenant, revisions, org_header, project_header):
    customer = None
    for project in dict.fromkeys(row.project_id for row in revisions):
        _headers(tenant, project, org_header, project_header)
        organization, _ = context_service._access(tenant, project, write=False)
        if customer is not None and customer != str(organization):
            context_service.refuse('CONTEXT_NOT_FOUND')
        customer = str(organization)
    return customer


def _read_pins(store, revisions, customer):
    return {
        project: store.application_record(str(project), customer)
        for project in dict.fromkeys(row.project_id for row in revisions)
    }


def _read_profiles(revisions, customer):
    with platform_link.platform_db().transaction(isolation='repeatable read', read_only=True) as connection:
        store = PostgresProfileStore(connection, customer)
        latest = store.latest(customer)
        pins = _read_pins(store, revisions, customer)
        windows = {}
        for revision in revisions:
            pin = pins[revision.project_id]
            if pin is None or latest is None or pin['version'] >= latest['version']:
                continue
            record = connection.execute(
                'SELECT design_window_json FROM x07_confirmation_evidence '
                'WHERE project_key=%s AND drawing_key=%s AND input_version_id=%s',
                (str(revision.project_id), str(revision.drawing_id), str(revision.input_version_id)),
            ).fetchone()
            if record is not None:
                windows[revision] = json.loads(record['design_window_json'])
    return latest, pins, windows


def _compare_roles(pin, latest):
    pinned = _fields(pin['roles'])
    current = _fields(latest['roles'])
    return [
        {'role': role, 'field': field or None, 'pinned': pinned.get((role, field)),
         'current': current.get((role, field))}
        for role, field in sorted(pinned.keys() | current.keys())
    ]


def _resolve_context(tenant, revision, customer):
    try:
        context = context_service.resolve_context(
            tenant, revision.project_id, revision.input_version_id,
            drawing_id=revision.drawing_id, write=False)
    except context_service.ProjectContextError as exc:
        if exc.reason_code in _UNAVAILABLE:
            return None, exc.reason_code
        raise
    if str(context.binding.organization_id) != customer:
        context_service.refuse('CONTEXT_NOT_FOUND')
    return context, None


def _resolve_revision(tenant, revision, customer):
    context, unavailable = _resolve_context(tenant, revision, customer)
    return (None if context is None else context.intake), unavailable


def _resolve_preview_contexts(tenant, project_id, drawing_id, previous_id, current_id,
                             org_header, project_header):
    revisions = [
        DrawingRevision(project_id=project_id, drawing_id=drawing_id, input_version_id=version)
        for version in dict.fromkeys((previous_id, current_id))
    ]
    customer = _authorized_customer(tenant, revisions, org_header, project_header)
    contexts = {}
    for revision in revisions:
        context, unavailable = _resolve_context(tenant, revision, customer)
        if unavailable:
            raise HTTPException(409, {'reason_code': unavailable})
        contexts[revision.input_version_id] = context
    return contexts[previous_id], contexts[current_id]


def _resolve_preview_intakes(tenant, project_id, drawing_id, previous_id, current_id,
                             org_header, project_header):
    previous, current = _resolve_preview_contexts(
        tenant, project_id, drawing_id, previous_id, current_id, org_header, project_header)
    return previous.intake, current.intake


@router.post(PANEL_GROUP_CHANGE_PREVIEW_PATH)
def preview_panel_group_changes(project_id: UUID, drawing_id: UUID, input_version_id: UUID,
                                payload: PanelGroupChangeRequest,
                                tenant=Depends(deps.require_active_tenant),
                                x_org_id: Optional[UUID] = Header(None),
                                x_project_id: Optional[UUID] = Header(None)):
    def operation():
        previous, current = _resolve_preview_contexts(
            tenant, project_id, drawing_id, payload.previous_input_version_id, input_version_id,
            x_org_id, x_project_id)
        try:
            return compare_panel_groups(previous, current)
        except GraphValidationError as exc:
            reason = exc.code if exc.code.startswith('PANEL_GROUP_') else 'PANEL_GROUP_GRAPH_UNAVAILABLE'
            raise HTTPException(409, {'reason_code': reason}) from exc

    return _dispatch(operation)


@router.post(PLACEMENT_OVERRIDE_PREVIEW_PATH)
def preview_placement_overrides(project_id: UUID, drawing_id: UUID, input_version_id: UUID,
                                payload: PlacementOverridePreviewRequest,
                                tenant=Depends(deps.require_active_tenant),
                                x_org_id: Optional[UUID] = Header(None),
                                x_project_id: Optional[UUID] = Header(None)):
    def operation():
        previous_id = payload.previous_input_version_id
        previous, current = _resolve_preview_intakes(
            tenant, project_id, drawing_id, previous_id, input_version_id, x_org_id, x_project_id)
        try:
            comparisons = [
                compare_placement_override(
                    previous, current, block=payload.block,
                    layer=payload.layer, window=override.model_dump(),
                )
                for override in payload.overrides
            ]
        except SelectionError as exc:
            raise HTTPException(409, {'reason_code': 'PLACEMENT_OVERRIDE_INVALID'}) from exc
        return {'previous_input_version_id': str(previous_id), 'input_version_id': str(input_version_id),
                'overrides': comparisons}

    return _dispatch(operation)


def _compatibility(intake, profile, customer):
    return check_compatibility(intake, profile, customer_key=customer, propose=lambda _intake: None)


def _decision(compatibility):
    if compatibility['decision'] == 'apply':
        return 'compatible', None
    reason = compatibility['reason']
    return ('unavailable' if reason == 'DRAWING_UNREADABLE' else 'needs_choice'), reason


def _placement_paths(intake, role, window):
    selected = select_modules(intake, block=role['block'], layer=role['layer'], window=window)
    if 'placements' in selected:
        paths = [placement['path'] for placement in selected['placements']]
    else:
        paths = [f'{handle.upper()}[0,0]' if HANDLE.fullmatch(handle) else None
                 for handle in selected['handles']]
    unique = set(paths)
    if None in unique or len(unique) != selected['count']:
        raise SelectionError('incomplete module placement identity')
    return unique


@router.post(PLACEMENT_CHANGE_PREVIEW_PATH)
def preview_placement_changes(project_id: UUID, drawing_id: UUID, input_version_id: UUID,
                              payload: PlacementChangePreviewRequest,
                              tenant=Depends(deps.require_active_tenant),
                              x_org_id: Optional[UUID] = Header(None),
                              x_project_id: Optional[UUID] = Header(None)):
    def operation():
        previous_id = payload.previous_input_version_id
        previous, current = _resolve_preview_intakes(
            tenant, project_id, drawing_id, previous_id, input_version_id, x_org_id, x_project_id)
        role = {'block': payload.block, 'layer': payload.layer}
        previous_role = role if payload.previous_role is None else payload.previous_role.model_dump()
        changes = []
        for window in payload.windows:
            within = window.model_dump()['within']
            try:
                before = _placement_paths(previous, previous_role, within)
                after = _placement_paths(current, role, within)
            except SelectionError as exc:
                raise HTTPException(409, {'reason_code': 'PLACEMENT_IDENTITY_UNAVAILABLE'}) from exc
            added = after - before
            removed = before - after
            if len(added) + len(removed) > MAX_REPORTED_CHANGES:
                raise HTTPException(409, {'reason_code': 'PLACEMENT_CHANGE_LIMIT_EXCEEDED'})
            changes.append({'previous_count': len(before), 'current_count': len(after),
                            'count_delta': len(after) - len(before), 'retained_count': len(before & after),
                            'added_paths': sorted(added), 'removed_paths': sorted(removed)})
        return {'previous_input_version_id': str(previous_id), 'input_version_id': str(input_version_id),
                'windows': changes}

    return _dispatch(operation)


def _compare_panels(intake, pinned, latest, window):
    if window is None:
        return {'status': 'unavailable', 'reason': 'CONFIRMED_WINDOW_UNAVAILABLE'}
    if pinned['decision'] != 'apply' or latest['decision'] != 'apply':
        return {'status': 'unavailable', 'reason': 'PROFILE_REQUIRES_REVIEW'}
    try:
        before = _placement_paths(intake, pinned['roles']['module'], window)
        after = _placement_paths(intake, latest['roles']['module'], window)
    except SelectionError:
        return {'status': 'unavailable', 'reason': 'PLACEMENT_IDENTITY_UNAVAILABLE'}
    added, removed = len(after - before), len(before - after)
    return {'status': 'different' if added or removed else 'same', 'added': added, 'removed': removed}


@router.post(PORTFOLIO_PATH)
def reuse_review(payload: ReuseRequest, tenant=Depends(deps.require_active_tenant),
                 x_org_id: Optional[UUID] = Header(None), x_project_id: Optional[UUID] = Header(None)):
    def operation():
        customer = _authorized_customer(tenant, payload.revisions, x_org_id, x_project_id)
        latest, pins, windows = _read_profiles(payload.revisions, customer)
        current_version = latest['version'] if latest else None
        rows = []
        for revision in payload.revisions:
            pin = pins[revision.project_id]
            profile = pin or latest
            older_pin = pin is not None and current_version is not None and pin['version'] < current_version
            intake, unavailable = _resolve_revision(tenant, revision, customer)
            panel_comparison = None
            if unavailable:
                status, reason = 'unavailable', unavailable
                if older_pin:
                    panel_comparison = {'status': 'unavailable', 'reason': unavailable}
            else:
                compatibility = _compatibility(intake, profile, customer)
                status, reason = _decision(compatibility)
                if older_pin:
                    current = _compatibility(intake, latest, customer)
                    panel_comparison = _compare_panels(intake, compatibility, current, windows.get(revision))
            rows.append({**revision.model_dump(mode='json'), 'status': status, 'reason': reason,
                         'profile_id': profile['profile_id'] if profile else None,
                         'profile_version': profile['version'] if profile else None,
                         'pinned': pin is not None,
                         'role_comparison': _compare_roles(pin, latest) if older_pin else None,
                         'panel_comparison': panel_comparison})
        return {'customer_key': customer, 'current_profile_version': current_version, 'rows': rows}

    return _dispatch(operation)


@router.post(REMOVED_ROLES_EXPORT_PATH)
def export_removed_roles(payload: RemovedRolesExportRequest, tenant=Depends(deps.require_active_tenant),
                         x_org_id: Optional[UUID] = Header(None), x_project_id: Optional[UUID] = Header(None)):
    def operation():
        customer = _authorized_customer(tenant, payload.revisions, x_org_id, x_project_id)
        profile_id = str(payload.profile_id)
        with platform_link.platform_db().transaction(isolation='repeatable read', read_only=True) as connection:
            row = connection.execute(
                'SELECT * FROM x07_profile_versions WHERE customer_key=%s AND profile_id=%s AND version=%s',
                (customer, profile_id, payload.through_version),
            ).fetchone()
            if row is None:
                raise HTTPException(404, 'Profile history version unavailable.')
            profile = CustomerProfileStore._profile(dict(row))
            pins = _read_pins(PostgresProfileStore(connection, customer), payload.revisions, customer)

        rows = []
        for revision in payload.revisions:
            _, unavailable = _resolve_revision(tenant, revision, customer)
            pin = pins[revision.project_id]
            removed = []
            if unavailable:
                status = 'unavailable'
            elif pin is None:
                status = 'unpinned'
            elif pin['profile_id'] != profile_id or pin['version'] > payload.through_version:
                status = 'outside_snapshot'
            else:
                removed = [{'role': role, 'applied': pin['roles'][role]}
                           for role in sorted(pin['roles'].keys() - profile['roles'].keys())]
                status = 'uses_removed_roles' if removed else 'no_removed_roles'
            rows.append({**revision.model_dump(mode='json'), 'status': status, 'reason': unavailable,
                         'pinned_profile_version': pin['version'] if pin else None, 'removed_roles': removed})
        return {'customer_key': customer, 'profile_id': profile_id, 'through_version': payload.through_version,
                'rows': rows}

    response = _dispatch(operation)
    if response.status_code == 200:
        response.headers['Content-Disposition'] = 'attachment; filename="removed-profile-roles.json"'
    return response
