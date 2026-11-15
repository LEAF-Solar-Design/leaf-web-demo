"""Pure customer-scoped role compatibility. Aliases are explicit drawing input.

Anonymous names are not stable identifiers: even an identical anonymous name
requires an injected alias. No customer identity is inferred from drawing data.
"""
import copy
import math
from solar_drawing_roles import discover_roles


def check_compatibility(intake, profile, *, customer_key, block_aliases=None, propose=discover_roles):
    if not isinstance(customer_key, str) or not customer_key or customer_key != customer_key.strip():
        raise ValueError('explicit customer scope is required')

    def fresh(reason):
        return {'schema': 'leaf.x07.profile-compatibility.v1', 'customer_key': customer_key,
                'decision': 'propose_fresh', 'reason': reason, 'roles': None,
                'applied_profile': None, 'proposal': propose(copy.deepcopy(intake))}

    if profile is None:
        return fresh('NO_PROFILE')
    if not isinstance(profile, dict) or profile.get('customer_key') != customer_key:
        return fresh('CUSTOMER_SCOPE_MISMATCH')
    if (not isinstance(profile.get('profile_id'), str) or not profile['profile_id']
            or type(profile.get('version')) is not int or profile['version'] < 1):
        return fresh('INVALID_PROFILE')
    roles = profile.get('roles')
    if (not isinstance(roles, dict) or 'module' not in roles
            or set(roles) - {'module', 'roof', 'obstruction'}):
        return fresh('INVALID_ROLE_MAP')
    if not isinstance(intake, dict) or not isinstance(intake.get('inserts'), list):
        return fresh('DRAWING_UNREADABLE')
    pairs = set()
    layers = set()
    for row in intake['inserts']:
        if (isinstance(row, dict) and isinstance(row.get('name'), str)
                and isinstance(row.get('layer'), str)
                and all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in ('x', 'y'))):
            pairs.add((row['name'], row['layer']))
            layers.add(row['layer'])
    for row in intake.get('polylines', []) if isinstance(intake.get('polylines', []), list) else []:
        if isinstance(row, dict) and isinstance(row.get('layer'), str):
            layers.add(row['layer'])
    aliases = {}
    if block_aliases is not None:
        if not isinstance(block_aliases, dict):
            return fresh('INVALID_ALIAS_MAP')
        for source, targets in block_aliases.items():
            targets = [targets] if isinstance(targets, str) else targets
            if (not isinstance(source, str) or not source or not isinstance(targets, (list, tuple))
                    or not targets or not all(isinstance(target, str) and target for target in targets)):
                return fresh('INVALID_ALIAS_MAP')
            aliases.setdefault(source.casefold(), set()).update(target.casefold() for target in targets)
    resolved = {}
    for role, wanted in roles.items():
        if (not isinstance(wanted, dict) or not isinstance(wanted.get('layer'), str) or not wanted['layer']
                or (role == 'module' and not wanted.get('block'))):
            return fresh('INVALID_ROLE_MAP')
        layer = wanted['layer'].casefold()
        block = wanted.get('block')
        if block is None:
            matches = sorted(value for value in layers if value.casefold() == layer)
            if not matches:
                return fresh('ROLE_NOT_FOUND')
            resolved[role] = {'layer': matches[0]}
            continue
        if not isinstance(block, str) or not block:
            return fresh('INVALID_ROLE_MAP')
        if block.startswith('*'):
            targets = aliases.get(block.casefold())
            if not targets:
                return fresh('ANONYMOUS_ALIAS_REQUIRED')
        else:
            targets = {block.casefold()}
        matches = sorted({(name, value) for name, value in pairs
                          if name.casefold() in targets and value.casefold() == layer})
        logical = {(name.casefold(), value.casefold()) for name, value in matches}
        if not logical:
            return fresh('ROLE_NOT_FOUND')
        if len(logical) != 1:
            return fresh('AMBIGUOUS_ALIAS')
        resolved[role] = {'block': matches[0][0], 'layer': matches[0][1]}
    return {'schema': 'leaf.x07.profile-compatibility.v1', 'customer_key': customer_key,
            'decision': 'apply', 'reason': None, 'roles': resolved,
            'applied_profile': {key: profile[key] for key in ('customer_key', 'profile_id', 'version')},
            'proposal': None}
