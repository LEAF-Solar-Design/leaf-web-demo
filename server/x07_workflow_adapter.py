"""Offline callback adapter bound to caller-injected, trusted drawing context.

This is not authorization or a route. The future host must authorize the context
before construction. UI callback keys cannot replace the injected identity.
"""
from copy import deepcopy
from x07_customer_profile_store import _key, ProfileScopeError
from x07_role_selection import select_modules


class WorkflowAdapter:
    def __init__(self, workflow, *, context, intake, copies, block_aliases=None):
        self.workflow = workflow
        if not isinstance(context, dict) or set(context) != {'customer_key', 'project_key', 'drawing_key'}:
            raise ValueError('inject all three context keys')
        self._context = {key: _key(value) for key, value in context.items()}
        self._intake, self._aliases = deepcopy(intake), deepcopy(block_aliases)
        if not isinstance(copies, list):
            raise ValueError('copy candidates must be a list')
        self._copies = deepcopy(copies)
        for row in self._copies:
            if not isinstance(row, dict) or type(row.get('oracle_count')) is not int or row['oracle_count'] < 0:
                raise ValueError('copy requires independent nonnegative count')
            selected = select_modules(self._intake, block=row.get('block'), layer=row.get('layer'), window=row.get('design_window'))
            if selected['count'] != row['oracle_count']:
                raise ValueError('copy count disagrees with independent oracle')

    def _scope(self, context):
        if context != self._context:
            raise ProfileScopeError('callback context does not match injected drawing scope')
        return dict(self._context)

    def _choice(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('explicit copy choice is required')
        if any(key in payload and payload[key] != value for key, value in self._context.items()):
            raise ProfileScopeError('choice cannot replace injected scope')
        for row in self._copies:
            if (isinstance(payload.get('block'), str) and isinstance(payload.get('layer'), str)
                    and payload['block'].casefold() == row['block'].casefold()
                    and payload['layer'].casefold() == row['layer'].casefold()
                    and payload.get('design_window') == row['design_window']):
                return deepcopy(row)
        raise ValueError('choice must match an injected copy candidate')

    def load_workflow(self, context):
        scope = self._scope(context)
        plan = self.workflow.inspect(deepcopy(self._intake), **scope, block_aliases=deepcopy(self._aliases))
        compatible = plan['compatibility']
        rows = self._copies
        if compatible['decision'] == 'apply':
            role = compatible['roles']['module']
            rows = [row for row in rows if row['block'].casefold() == role['block'].casefold()
                    and row['layer'].casefold() == role['layer'].casefold()]
        return {**plan, 'copies': deepcopy(rows)}

    def confirm_roles(self, context, payload):
        scope = self._scope(context)
        choice = self._choice(payload)
        # The UI payload represents an explicit module choice. Optional roof or
        # obstruction editing remains a separate injected profile-editor event.
        return self.workflow.confirm(deepcopy(self._intake), **scope,
            roles={'module': {'block': choice['block'], 'layer': choice['layer']}},
            window=choice['design_window'], expected_version=payload.get('expected_version'))

    def apply_profile(self, context, payload):
        scope = self._scope(context)
        choice = self._choice(payload)
        plan = self.load_workflow(context)
        compatible = plan['compatibility']
        if compatible['decision'] == 'apply':
            role = compatible['roles']['module']
            if (choice['block'].casefold(), choice['layer'].casefold()) != (role['block'].casefold(), role['layer'].casefold()):
                raise ValueError('copy choice does not match compatible module role')
        return self.workflow.apply(deepcopy(self._intake), **scope,
            window=choice['design_window'], block_aliases=deepcopy(self._aliases))
