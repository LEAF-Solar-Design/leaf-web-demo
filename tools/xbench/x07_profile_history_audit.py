"""Read-only X07 customer history audit: exit0 clean,1 findings,2 unavailable."""
import argparse
from pathlib import Path
import sqlite3
import stat
from x07_profile_version_diff import CustomerProfileStore, MAX_PROFILE_VERSION, _key, publish_stdout
from x07_customer_profile_store import _check_connection

MAX_AUDIT_ROWS = 10000


def require_database_file(path):
    if not stat.S_ISREG(Path(path).lstat().st_mode):
        raise ValueError('regular database file required')


def audit_history(store, *, customer_key):
    customer_key = _key(customer_key)
    if store.read_only is not True:
        raise ValueError('explicit read-only store required')
    require_database_file(store.path)
    with store._connection() as db:
        if db.row_factory is not sqlite3.Row:
            raise ValueError('named SQLite rows required')
        _check_connection(db)
        db.execute('BEGIN')
        root = db.execute('SELECT profile_id FROM customers WHERE customer_key=?', (customer_key,)).fetchone()
        versions = db.execute('SELECT version,profile_id FROM profile_versions WHERE customer_key=? ORDER BY version LIMIT ?',
                              (customer_key, MAX_AUDIT_ROWS + 1)).fetchall()
        pins = db.execute('SELECT version,profile_id FROM project_applications WHERE customer_key=? ORDER BY project_key LIMIT ?',
                          (customer_key, MAX_AUDIT_ROWS + 1)).fetchall()
    if len(versions) > MAX_AUDIT_ROWS or len(pins) > MAX_AUDIT_ROWS:
        raise ValueError('audit row budget exceeded')
    profile_id = root['profile_id'] if root else None
    numbers = sorted({row['version'] for row in versions
                      if type(row['version']) is int and 1 <= row['version'] <= MAX_PROFILE_VERSION})
    gaps, next_version = [], 1
    for version in numbers:
        if version > next_version:
            gaps.append({'first': next_version, 'last': version - 1})
        next_version = version + 1
    present = set(numbers)
    issues = {'missing_profile': int(root is None), 'empty_chain': int(root is not None and not versions), 'version_gaps': gaps,
              'invalid_versions': sum(type(row['version']) is not int or not 1 <= row['version'] <= MAX_PROFILE_VERSION for row in versions),
              'foreign_version_profiles': sum(row['profile_id'] != profile_id for row in versions),
              'dangling_pin_versions': sum(type(row['version']) is not int
                                          or not 1 <= row['version'] <= MAX_PROFILE_VERSION
                                          or row['version'] not in present for row in pins),
              'foreign_pin_profiles': sum(row['profile_id'] != profile_id for row in pins)}
    return {'schema': 'leaf.x07.profile-history-audit.v1', 'customer_key': customer_key,
            'profile_found': root is not None, 'version_count': len(versions), 'pin_count': len(pins),
            'healthy': not any(issues.values()), 'issues': issues}


class AuditParser(argparse.ArgumentParser):
    def error(self, message):
        publish_stdout({'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'INVALID_ARGUMENTS'})
        raise SystemExit(2)


def main():
    parser = AuditParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--customer', required=True)
    args = parser.parse_args()
    try:
        require_database_file(args.database)
        result = audit_history(CustomerProfileStore(args.database, read_only=True), customer_key=args.customer)
    except (OSError, ValueError, sqlite3.Error):
        publish_stdout({'schema': 'leaf.x07.profile-history-audit.v1', 'error': 'AUDIT_UNAVAILABLE'})
        return 2
    if publish_stdout(result):
        return 2
    return 0 if result['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
