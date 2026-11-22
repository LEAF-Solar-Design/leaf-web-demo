"""Offline service over injected drawing intake and customer store. No routes.

Inspect is read-only. Confirm validates and counts before saving; apply rechecks
compatibility and pins exactly the checked version or refuses concurrent drift.
A saved version can survive a failed pin and is reusable on the next inspect.
"""
from copy import deepcopy
from x07_customer_profile_store import _key
from x07_profile_compatibility import check_compatibility
from x07_role_selection import select_modules


class ProfileWorkflow:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _context(customer_key, project_key, drawing_key):
        return {name: _key(value) for name, value in
                [('customer_key', customer_key), ('project_key', project_key), ('drawing_key', drawing_key)]}

    def inspect(self, intake, *, customer_key, project_key, drawing_key, block_aliases=None):
        context = self._context(customer_key, project_key, drawing_key)
        pin = self.store.application_record(project_key, customer_key)
        profile = pin or self.store.latest(customer_key)
        compatibility = check_compatibility(intake, profile, customer_key=customer_key, block_aliases=block_aliases)
        return {'schema': 'leaf.x07.profile-workflow.v1', **context,
                'compatibility': compatibility, 'application_record': pin,
                'expected_version': (self.store.latest(customer_key) or {}).get('version', 0)}

    def apply(self, intake, *, customer_key, project_key, drawing_key, window, block_aliases=None):
        plan = self.inspect(intake, customer_key=customer_key, project_key=project_key,
                            drawing_key=drawing_key, block_aliases=block_aliases)
        compatible = plan['compatibility']
        if compatible['decision'] != 'apply':
            return {**plan, 'state': 'needs_confirmation', 'applied_profile': None, 'selection': None}
        role = compatible['roles']['module']
        selection = select_modules(intake, block=role['block'], layer=role['layer'], window=window)
        version = compatible['applied_profile']['version']
        pin = self.store.apply_to_project(project_key, customer_key, expected_version=version)
        return self._result(plan, pin, compatible['roles'], selection)

    def confirm(self, intake, *, customer_key, project_key, drawing_key, roles, window, expected_version):
        context = self._context(customer_key, project_key, drawing_key)
        if self.store.application_record(project_key, customer_key) is not None:
            raise ValueError('project already pinned; apply its existing version')
        # Explicit confirmation permits the current literal anonymous name. Later
        # drawings still require their caller-supplied alias correspondence.
        aliases = {r['block']: r['block'] for r in roles.values()
                   if isinstance(r, dict) and isinstance(r.get('block'), str) and r['block'].startswith('*')} if isinstance(roles, dict) else {}
        candidate = {'customer_key': customer_key, 'profile_id': 'unpersisted-confirmation',
                     'version': 1, 'roles': deepcopy(roles)}
        compatible = check_compatibility(intake, candidate, customer_key=customer_key, block_aliases=aliases)
        if compatible['decision'] != 'apply':
            raise ValueError('confirmed roles are incompatible: ' + compatible['reason'])
        role = compatible['roles']['module']
        selection = select_modules(intake, block=role['block'], layer=role['layer'], window=window)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('explicit expected version is required')
        saved = self.store.save_version(customer_key, compatible['roles'], expected_version=expected_version)
        pin = self.store.apply_to_project(project_key, customer_key, expected_version=saved['version'])
        return self._result(context, pin, compatible['roles'], selection)

    @staticmethod
    def _result(context, pin, roles, selection):
        return {'schema': 'leaf.x07.profile-workflow.v1', 'state': 'applied',
                **{key: context[key] for key in ('customer_key', 'project_key', 'drawing_key')},
                'applied_profile': deepcopy(pin), 'resolved_roles': deepcopy(roles), 'selection': selection}
