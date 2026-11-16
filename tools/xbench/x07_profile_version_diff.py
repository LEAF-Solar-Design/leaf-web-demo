"""Read-only X07 role differences between two explicitly scoped versions."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from x07_customer_profile_store import CustomerProfileStore, MAX_PROFILE_VERSION, _key


def compare_versions(store, *, customer_key, profile_id, before_version, after_version):
    customer_key, profile_id = _key(customer_key), _key(profile_id)
    if any(type(value) is not int or not 1 <= value <= MAX_PROFILE_VERSION
           for value in (before_version, after_version)):
        raise ValueError('explicit positive safe version numbers are required')
    with store._connection() as db:
        rows = db.execute('SELECT * FROM profile_versions WHERE customer_key=? AND profile_id=? AND version IN (?,?)',
                          (customer_key, profile_id, before_version, after_version)).fetchall()
        versions = {row['version']: store._profile(row)['roles'] for row in rows}
    if before_version not in versions or after_version not in versions:
        raise ValueError('scoped versions unavailable')
    before, after = versions[before_version], versions[after_version]
    return {'schema': 'leaf.x07.profile-version-diff.v1', 'customer_key': customer_key, 'profile_id': profile_id,
            'before_version': before_version, 'after_version': after_version,
            'added': {role: copy.deepcopy(after[role]) for role in sorted(after.keys() - before.keys())},
            'removed': {role: copy.deepcopy(before[role]) for role in sorted(before.keys() - after.keys())},
            'changed': {role: {'before': copy.deepcopy(before[role]), 'after': copy.deepcopy(after[role])}
                        for role in sorted(before.keys() & after.keys()) if before[role] != after[role]}}


class VersionDiffParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'schema': 'leaf.x07.profile-version-diff.v1', 'error': 'INVALID_ARGUMENTS'}))
        raise SystemExit(2)


def main():
    parser = VersionDiffParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--customer', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--before', type=int, required=True)
    parser.add_argument('--after', type=int, required=True)
    args = parser.parse_args()
    try:
        result = compare_versions(CustomerProfileStore(args.database, read_only=True), customer_key=args.customer,
                                  profile_id=args.profile_id, before_version=args.before, after_version=args.after)
    except (ValueError, sqlite3.Error):
        print(json.dumps({'schema': 'leaf.x07.profile-version-diff.v1', 'error': 'SCOPED_VERSIONS_UNAVAILABLE'}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
