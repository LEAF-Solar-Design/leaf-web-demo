"""Five synthetic drawings through X07's isolated Python primitives, never LIVE."""
import argparse
import json
import hashlib
import subprocess
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from solar_drawing_roles import discover_roles
from x07_customer_profile_store import CustomerProfileStore
from x07_profile_compatibility import check_compatibility
from x07_role_selection import select_modules


SOURCE_FILES = ['tools/xbench/x07_synthetic_journey.py', 'server/solar_drawing_roles.py',
                'server/x07_customer_profile_store.py', 'server/x07_profile_compatibility.py',
                'server/x07_role_selection.py']


def source_snapshot(root):
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=20).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, timeout=20).strip())
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    return {'source_sha': commit, 'source_dirty': dirty, 'files': files}


def run(database):
    root = Path(__file__).resolve().parents[2]
    source = source_snapshot(root)
    if Path(database).exists():
        raise ValueError('synthetic journey requires a new database')
    store = CustomerProfileStore(database)
    steps = []
    def checked(number, **assertions):
        if not all(value is True for value in assertions.values()):
            raise AssertionError((number, assertions))
        steps.append({'n': number, 'passed': True, 'assertions': assertions})
    def drawing(block, layer, points):
        return {'inserts': [{'name': block, 'layer': layer, 'x': x, 'y': y} for x, y in points]}
    # Oracles are explicit point lists and counts, independent of product output.
    a1 = drawing('Panel A', 'MODULES A', [(0, 0), (1, 1), (2, 0)])
    a2 = drawing('Panel A', 'MODULES A', [(0, 0), (1, 0), (2, 0), (3, 0)])
    a3 = drawing('Panel A', 'MODULES A', [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)])
    copies = [(offset + x, 0) for offset in (0, 10, 20) for x in (0, 1)]
    b1 = drawing('*U1', 'MODULES B', copies)
    b2 = drawing('*U2', 'MODULES B', copies)
    b1['inserts'] += drawing('Panel competitor', '0', [(100, 0), (101, 0), (102, 0), (103, 0)])['inserts']
    first = check_compatibility(a1, None, customer_key='synthetic-a')
    picked_a = select_modules(a1, block='Panel A', layer='MODULES A', window=[0, 0, 2, 1])
    checked(1, fresh_proposal=first['decision'] == 'propose_fresh',
            correct_first_choice=first['proposal']['module']['first_choice']['block'] == 'Panel A',
            confirmation_model_count=picked_a['count'] == 3)
    proposed_b = discover_roles(b1)
    picked_b = select_modules(b1, block='*U1', layer='MODULES B', window=[20, 0, 21, 0])
    checked(2, competing_sets=proposed_b['module']['competing'],
            counts_match_oracles=sorted(row['count'] for row in proposed_b['module']['candidates']) == [4, 6],
            one_copy_count=picked_b['count'] == 2)
    profile_a = store.save_version('synthetic-a', {'module': {'block': 'Panel A', 'layer': 'MODULES A'}}, expected_version=0)
    profile_b = store.save_version('synthetic-b', {'module': {'block': '*U1', 'layer': 'MODULES B'}}, expected_version=0)
    checked(3, customer_scopes_differ=profile_a['customer_key'] != profile_b['customer_key'],
            profile_ids_differ=profile_a['profile_id'] != profile_b['profile_id'],
            initial_versions=profile_a['version'] == profile_b['version'] == 1)
    reused = []
    for project, intake, expected in [('a-next-one', a2, 4), ('a-next-two', a3, 5)]:
        compatible = check_compatibility(intake, store.latest('synthetic-a'), customer_key='synthetic-a')
        assert compatible['decision'] == 'apply'
        applied = store.apply_to_project(project, 'synthetic-a')
        role = compatible['roles']['module']
        selected = select_modules(intake, block=role['block'], layer=role['layer'], window=[0, 0, 4, 1])
        reused.append(applied['profile_id'] == profile_a['profile_id'] and selected['count'] == expected)
    checked(4, both_counts_match=all(reused), no_profile_edits=store.latest('synthetic-a')['version'] == 1)
    compatible_b = check_compatibility(b2, profile_b, customer_key='synthetic-b', block_aliases={'*U1': '*U2'})
    assert compatible_b['decision'] == 'apply'
    applied_b = store.apply_to_project('b-next', 'synthetic-b')
    role_b = compatible_b['roles']['module']
    selection_b = select_modules(b2, block=role_b['block'], layer=role_b['layer'], window=[10, 0, 11, 0])
    checked(5, own_profile=applied_b['profile_id'] == profile_b['profile_id'],
            alias_explicit=role_b['block'] == '*U2', single_copy_count=selection_b['count'] == 2)
    wrong_a = check_compatibility(a3, profile_b, customer_key='synthetic-b', block_aliases={'*U1': '*U2'})
    wrong_b = check_compatibility(b2, profile_a, customer_key='synthetic-a')
    foreign = check_compatibility(a1, profile_a, customer_key='synthetic-b')
    checked(6, both_incompatible_drawings_fresh=wrong_a['decision'] == wrong_b['decision'] == 'propose_fresh',
            no_applied_foreign_profile=wrong_a['applied_profile'] is None and wrong_b['applied_profile'] is None,
            explicit_scope_refused=foreign['reason'] == 'CUSTOMER_SCOPE_MISMATCH',
            no_wrong_application=store.application_record('cross-a-under-b', 'synthetic-b') is None)
    edited = store.save_version('synthetic-a', {'module': {'block': 'Panel A', 'layer': 'REVISED MODULES A'}}, expected_version=1)
    old = store.application_record('a-next-one', 'synthetic-a')
    new = store.apply_to_project('a-after-edit', 'synthetic-a')
    checked(7, new_version=edited['version'] == 2, stable_profile_id=edited['profile_id'] == profile_a['profile_id'],
            old_project_retains_version=old['version'] == 1 and old['roles'] == profile_a['roles'],
            new_project_pins_latest=new['version'] == 2)
    if source_snapshot(root) != source:
        raise ValueError('source changed during synthetic journey')
    return {'schema': 'leaf.x07.synthetic-journey.v1', 'proof_mode': 'synthetic_offline', **source,
            'drawings': 5, 'customers': 2, 'steps': steps,
            'limits': ['No live routes, authentication, UI interactions, real drawings or staging readiness checked.',
                       'Anonymous aliases and design-copy windows are explicit injected inputs.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.database:
        result = run(args.database)
    else:
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp) / 'journey.sqlite')
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
