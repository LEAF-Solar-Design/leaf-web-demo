"""Project-authorized customer roles on the canonical PostgreSQL store."""
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, StrictStr, field_validator

import deps
import platform_link
import solar_project_context as context_service
from routers.project_drawings import _headers, _dispatch
from envelopes import with_envelope_fields
from x07_customer_profile_store import TrackerRole, validate_trackers
from x07_postgres_profile_store import PostgresProfileStore
from x07_profile_compatibility import check_compatibility
from x07_profile_workflow import ProfileWorkflow
from x07_role_selection import select_modules

router = APIRouter()
RECOVERY_PATH = '/api/role-profiles/recovery-report'
PATH = '/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/role-profile'


async def no_store_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith('/role-profile') or request.url.path == RECOVERY_PATH:
        response.headers['Cache-Control'] = 'no-store'
    return response


class Role(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block: Optional[StrictStr] = Field(default=None, min_length=1, max_length=512)
    layer: StrictStr = Field(min_length=1, max_length=512)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block: StrictStr = Field(min_length=1, max_length=512)
    layer: StrictStr = Field(min_length=1, max_length=512)
    design_window: list[FiniteFloat] = Field(min_length=4, max_length=4)
    expected_version: StrictInt = Field(ge=0, le=2**53 - 2)

    @field_validator('design_window', mode='before')
    @classmethod
    def numeric_window(cls, value):
        if not isinstance(value, list) or any(type(item) not in (int, float) for item in value):
            raise ValueError('numeric coordinates required')
        return value


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    roles: dict[StrictStr, Role | list[TrackerRole]] = Field(min_length=1, max_length=4)

    @field_validator('roles', mode='before')
    @classmethod
    def role_shape(cls, roles):
        if not isinstance(roles, dict) or 'module' not in roles or set(roles) - {'module','roof','obstruction','trackers'}:
            raise ValueError('invalid role map')
        if any(not isinstance(value, dict) for key, value in roles.items() if key != 'trackers'):
            raise ValueError('invalid drawing role')
        if 'trackers' in roles:
            validate_trackers(roles['trackers'])
        return roles

    expected_version: StrictInt = Field(ge=1, le=2**53 - 2)


def _operate(tenant, project_id, drawing_id, input_version_id, org_header, project_header, action, payload=None):
    def operation():
        # The project/version gate runs before any customer-profile query.
        _headers(tenant, project_id, org_header, project_header)
        context = context_service.resolve_context(tenant, project_id, input_version_id,
                                                  drawing_id=drawing_id, write=action != 'read')
        customer = str(context.binding.organization_id)
        project, drawing = str(project_id), str(drawing_id)
        identity = {'customer_key': customer, 'project_key': project, 'drawing_key': drawing,
                    'input_version_id': str(input_version_id)}
        with platform_link.platform_db().transaction() as connection:
            store = PostgresProfileStore(connection, customer)
            workflow = ProfileWorkflow(store)
            if action != 'read':
                store.lock()
            plan = workflow.inspect(context.intake, customer_key=customer,
                                    project_key=project, drawing_key=drawing)
            if action == 'read':
                return {**plan, **identity, 'profile': store.latest(customer)}
            if action == 'edit':
                roles = payload.model_dump(exclude_none=True)['roles']
                candidate = {'customer_key': customer, 'profile_id': 'explicit-edit',
                             'version': 1, 'roles': roles}
                aliases = {role['block']: role['block'] for role in roles.values() if isinstance(role, dict) and role.get('block', '').startswith('*')}
                compatible = check_compatibility(context.intake, candidate, customer_key=customer, block_aliases=aliases)
                if compatible['decision'] != 'apply':
                    raise ValueError('incompatible roles')
                return {**identity, 'profile': store.save_version(customer, compatible['roles'], expected_version=payload.expected_version)}
            roles = {'module': {'block': payload.block, 'layer': payload.layer}}
            pin = plan['application_record']
            if pin is not None:
                # Lost confirmation replies replay the original immutable pin.
                if pin['roles']['module'] != roles['module']:
                    raise ValueError('project already pinned')
                selection = select_modules(context.intake, block=payload.block, layer=payload.layer,
                                           window=payload.design_window)
                result = workflow._result(plan, pin, pin['roles'], selection)
            elif plan['compatibility']['decision'] == 'apply' and plan['compatibility']['roles']['module'] == roles['module']:
                if plan['expected_version'] != payload.expected_version:
                    raise ValueError('profile version changed')
                result = workflow.apply(context.intake, customer_key=customer, project_key=project,
                                        drawing_key=drawing, window=payload.design_window)
            else:
                result = workflow.confirm(context.intake, customer_key=customer, project_key=project,
                                          drawing_key=drawing, roles=roles, window=payload.design_window,
                                          expected_version=payload.expected_version)
            if pin is None:
                connection.execute(
                    'INSERT INTO x07_confirmation_evidence '
                    '(project_key, drawing_key, input_version_id, design_window_json) VALUES (%s, %s, %s, %s)',
                    (project, drawing, str(input_version_id), json.dumps(payload.design_window)),
                )
            # A replay can return an older project pin. Read the editable profile
            # separately under the same customer lock as confirmation.
            return {**result, **identity, 'ok': True, 'profile': store.latest(customer)}
    try:
        return JSONResponse(content=with_envelope_fields(operation()), headers={'Cache-Control': 'no-store'})
    except ValueError:
        return _refused()
    except context_service.ProjectContextError as error:
        def refused_context():
            raise error
        return _dispatch(refused_context)
    except Exception:
        return JSONResponse(status_code=503, headers={'Cache-Control': 'no-store'},
                            content={'ok': False, 'error': {'reason_code': 'X07_PROFILE_UNAVAILABLE',
                                                           'message': 'Customer profile operation is unavailable.'}})


def _refused():
    return JSONResponse(status_code=409, headers={'Cache-Control': 'no-store'},
                        content={'ok': False, 'error': {'reason_code': 'X07_PROFILE_CONFLICT',
                                                       'message': 'Customer profile operation could not be completed.'}})


@router.get(PATH)
def read(project_id: UUID, drawing_id: UUID, input_version_id: UUID,
         tenant=Depends(deps.require_active_tenant), x_org_id: Optional[UUID] = Header(default=None),
         x_project_id: Optional[UUID] = Header(default=None)):
    return _operate(tenant, project_id, drawing_id, input_version_id, x_org_id, x_project_id, 'read')


@router.post(PATH)
def confirm(project_id: UUID, drawing_id: UUID, input_version_id: UUID, payload: Confirmation,
            tenant=Depends(deps.require_active_tenant), x_org_id: Optional[UUID] = Header(default=None),
            x_project_id: Optional[UUID] = Header(default=None)):
    return _operate(tenant, project_id, drawing_id, input_version_id, x_org_id, x_project_id, 'confirm', payload)


@router.put(PATH)
def edit(project_id: UUID, drawing_id: UUID, input_version_id: UUID, payload: Edit,
         tenant=Depends(deps.require_active_tenant), x_org_id: Optional[UUID] = Header(default=None),
         x_project_id: Optional[UUID] = Header(default=None)):
    return _operate(tenant, project_id, drawing_id, input_version_id, x_org_id, x_project_id, 'edit', payload)


class RecoveryRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    project_id: UUID
    drawing_id: UUID
    input_version_id: UUID


class RecoveryReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revisions: list[RecoveryRevision] = Field(min_length=1, max_length=32)

    @field_validator('revisions')
    @classmethod
    def reject_duplicate_revisions(cls, revisions):
        if len(set(revisions)) != len(revisions):
            raise ValueError('duplicate recovery revision')
        return revisions


@router.post(RECOVERY_PATH)
def recovery_report(payload: RecoveryReportRequest, tenant=Depends(deps.require_active_tenant),
                    x_org_id: Optional[UUID] = Header(default=None),
                    x_project_id: Optional[UUID] = Header(default=None)):
    def operation():
        scopes = []
        for revision in payload.revisions:
            _headers(tenant, revision.project_id, x_org_id, x_project_id)
            context = context_service.resolve_context(
                tenant, revision.project_id, revision.input_version_id,
                drawing_id=revision.drawing_id, write=False)
            scopes.append((revision, str(context.binding.organization_id)))

        rows = []
        with platform_link.platform_db().transaction(isolation='repeatable read', read_only=True) as connection:
            for revision, customer in scopes:
                identity = revision.model_dump(mode='json')
                store = PostgresProfileStore(connection, customer)
                pin = store.application_record(identity['project_id'], customer)
                evidence = None
                if pin is not None:
                    record = connection.execute(
                        'SELECT design_window_json FROM x07_confirmation_evidence '
                        'WHERE project_key=%s AND drawing_key=%s AND input_version_id=%s',
                        (identity['project_id'], identity['drawing_id'], identity['input_version_id']),
                    ).fetchone()
                    if record is not None:
                        evidence = {'design_window': json.loads(record['design_window_json'])}
                rows.append({
                    **identity, 'customer_key': customer,
                    'profile_id': pin['profile_id'] if pin else None,
                    'pinned_version': pin['version'] if pin else None,
                    'evidence_status': 'confirmed' if evidence is not None else 'unavailable',
                    'evidence': evidence,
                })
        return {'ok': True, 'rows': rows}

    return _dispatch(operation)
