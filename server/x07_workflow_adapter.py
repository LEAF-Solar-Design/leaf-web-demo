"""Offline callback adapter bound to caller-injected, trusted drawing context.

This is not authorization or a route. The future host must authorize the context
before construction. UI callback keys cannot replace the injected identity.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from x07_customer_profile_store import _key, ProfileScopeError, CustomerProfileStore
from x07_profile_workflow import ProfileWorkflow
from x07_role_selection import select_modules
from x07_profile_compatibility import check_compatibility


class AdapterVersionConflict(ValueError):
    code = 'STALE_VERSION'


class IntakeBudgetError(ValueError):
    code = 'INTAKE_BUDGET_EXCEEDED'


def _distinct_store_paths(profile_path, receipt_path):
    profile = Path(profile_path).resolve()
    receipt = Path(receipt_path).resolve()
    if profile == receipt or (profile.exists() and receipt.exists() and profile.samefile(receipt)):
        raise ValueError('profile and receipt paths must be distinct files')


class ConfirmationReceiptStore:
    """Explicitly injected offline journal. Lost successful replies can replay.

    Receipt+profile are separate databases. A crash between profile success and
    journal commit still requires inspect/apply recovery, not blind confirmation.
    This is not a live authorization or distributed exactly-once guarantee.
    """
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        if type(read_only) is not bool:
            raise ValueError('read_only must be a boolean')
        self.read_only = read_only
        db = self._connect()
        try:
            with db:
                db.execute('BEGIN' if self.read_only else 'BEGIN IMMEDIATE')
                version = db.execute('PRAGMA user_version').fetchone()[0]
                ddl = 'CREATE TABLE confirmations (project_key TEXT PRIMARY KEY, customer_key TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL)'
                objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                if version not in (0, 1) or (objects and objects != [('table', 'confirmations', ddl)]):
                    raise ValueError('unsupported confirmation journal schema')
                if self.read_only and not objects:
                    raise ValueError('unsupported read-only confirmation journal schema')
                if not objects:
                    if version: raise ValueError('unsupported confirmation journal schema')
                    db.execute(ddl)
                if version == 0 and not self.read_only: db.execute('PRAGMA user_version=1')
        finally: db.close()

    def _connect(self):
        if self.read_only:
            return sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        return sqlite3.connect(self.path, timeout=5)

    def run(self, context, digest, produce, validate):
        db = self._connect()
        try:
            with db:
                db.execute('BEGIN' if self.read_only else 'BEGIN IMMEDIATE')
                row = db.execute('SELECT customer_key,digest,result FROM confirmations WHERE project_key=?',(context['project_key'],)).fetchone()
                if row:
                    if row[0] != context['customer_key']: raise ProfileScopeError('receipt belongs to another customer')
                    if row[1] != digest: raise ValueError('confirmation retry payload changed')
                    try:
                        if not isinstance(row[2], str) or len(row[2].encode('utf-8')) > 262144:
                            raise ValueError()
                        result = json.loads(row[2], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        if (not isinstance(result, dict) or result.get('schema') != 'leaf.x07.profile-workflow.v1'
                                or result.get('state') != 'applied'
                                or any(result.get(key) != value for key,value in context.items())
                                or not isinstance(result.get('applied_profile'), dict)
                                or result['applied_profile'].get('customer_key') != context['customer_key']):
                            raise ValueError()
                    except (ValueError, TypeError, RecursionError, UnicodeError):
                        raise ValueError('invalid confirmation receipt') from None
                    validate(result);return result
                if self.read_only:
                    raise ValueError('confirmation receipt unavailable in read-only journal')
                result = produce()
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?)',
                    (context['project_key'],context['customer_key'],digest,json.dumps(result,allow_nan=False)))
                return result
        finally: db.close()


def _budget(intake, copies, aliases, limits):
    caps = {'inserts':100000,'aliases':4096,'copies':256,'depth':32,'nodes':1000000,'string_bytes':16*1024*1024}
    if limits is not None:
        if not isinstance(limits,dict) or set(limits)-set(caps) or any(type(v) is not int or v<1 for v in limits.values()):
            raise ValueError('invalid offline intake budgets')
        caps.update(limits)
    for name,value in [('inserts',intake.get('inserts',[]) if isinstance(intake,dict) else []),('copies',copies),('aliases',aliases or {})]:
        if isinstance(value,(dict,list,tuple)) and len(value)>caps[name]:raise IntakeBudgetError(name+' budget exceeded')
    stack=[(value,0) for value in [intake,copies,aliases]];nodes=0;strings=0
    while stack:
        value,depth=stack.pop();nodes+=1
        if depth>caps['depth'] or nodes>caps['nodes']:raise IntakeBudgetError('structure budget exceeded')
        if isinstance(value,str):
            strings+=len(value.encode('utf-8'))
            if strings>caps['string_bytes']:raise IntakeBudgetError('string budget exceeded')
        elif isinstance(value,dict):
            if len(value)*2+nodes+len(stack)>caps['nodes']:raise IntakeBudgetError('structure budget exceeded')
            stack.extend((child,depth+1) for item in value.items() for child in item)
        elif isinstance(value,(list,tuple)):
            if len(value)+nodes+len(stack)>caps['nodes']:raise IntakeBudgetError('structure budget exceeded')
            stack.extend((child,depth+1) for child in value)
        elif value is not None and type(value) not in (int,float,bool):raise IntakeBudgetError('unsupported intake value')


class WorkflowAdapter:
    @classmethod
    def from_paths(cls, *, profile_path, receipt_path, context, intake, copies, block_aliases=None, limits=None):
        """Validate both caller-supplied paths before either schema is opened."""
        _distinct_store_paths(profile_path, receipt_path)
        store = CustomerProfileStore(profile_path)
        receipts = ConfirmationReceiptStore(receipt_path)
        return cls(ProfileWorkflow(store), context=context, intake=intake, copies=copies,
            block_aliases=block_aliases, limits=limits, confirmation_receipts=receipts)

    def __init__(self, workflow, *, context, intake, copies, block_aliases=None, limits=None, confirmation_receipts=None):
        if confirmation_receipts is not None:
            _distinct_store_paths(workflow.store.path, confirmation_receipts.path)
        _budget(intake,copies,block_aliases,limits)  # before snapshot copying
        self.workflow = workflow
        self._receipts = confirmation_receipts
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
        self._plan_token = hashlib.sha256(json.dumps([self._context, self._intake, self._copies, self._aliases],
            sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()

    def _scope(self, context):
        if context != self._context:
            raise ProfileScopeError('callback context does not match injected drawing scope')
        return dict(self._context)

    def _choice(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('explicit copy choice is required')
        if any(key in payload and payload[key] != value for key, value in self._context.items()):
            raise ProfileScopeError('choice cannot replace injected scope')
        if 'plan_token' in payload and payload['plan_token'] != self._plan_token:
            raise ValueError('drawing snapshot changed; reload workflow')
        for row in self._copies:
            if (isinstance(payload.get('block'), str) and isinstance(payload.get('layer'), str)
                    and payload['block'].casefold() == row['block'].casefold()
                    and payload['layer'].casefold() == row['layer'].casefold()
                    and payload.get('design_window') == row['design_window']):
                return {key: deepcopy(value) for key, value in row.items() if key != 'plan_token'}
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
        return {**plan, 'plan_token': self._plan_token, 'copies': deepcopy(rows)}

    def confirm_roles(self, context, payload):
        scope = self._scope(context)
        choice = self._choice(payload)
        # The UI payload represents an explicit module choice. Optional roof or
        # obstruction editing remains a separate injected profile-editor event.
        def produce():
            return self.workflow.confirm(deepcopy(self._intake), **scope,
                roles={'module': {'block': choice['block'], 'layer': choice['layer']}},
                window=choice['design_window'], expected_version=payload.get('expected_version'))
        if self._receipts is None: return produce()
        # Request identity ignores object-member order. Explicit label values,
        # expected version and snapshot token remain part of the saved request.
        # Keep this encoding stable for existing offline journal receipts.
        digest = hashlib.sha256(json.dumps([self._plan_token,payload],sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
        def validate(result):
            pin = self.workflow.store.application_record(scope['project_key'],scope['customer_key'])
            if pin != result.get('applied_profile'):raise ValueError('confirmation receipt no longer matches project pin')
            selection = select_modules(self._intake, block=choice['block'], layer=choice['layer'],
                                       window=choice['design_window'])
            expected = self.workflow._result(scope, pin, pin['roles'], selection)
            # Compare the complete producer shape with independently read pinned
            # roles and recounted geometry. JSON preserves bool/int distinctions.
            try:
                matches = json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                matches = False
            if not matches: raise ValueError('confirmation receipt payload is inconsistent')
        return self._receipts.run(scope,digest,produce,validate)

    def load_profile(self, customer_key):
        if customer_key != self._context['customer_key']:
            raise ProfileScopeError('editor customer does not match injected scope')
        # Also refuse an injected project which belongs to another customer.
        self.workflow.store.application_record(self._context['project_key'], customer_key)
        return self.workflow.store.latest(customer_key)

    def save_profile(self, customer_key, roles, *, expected_version):
        latest = self.load_profile(customer_key)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('editor expected version must be an explicit nonnegative integer')
        if not isinstance(roles, dict):
            raise ValueError('editor roles must be an object')
        candidate = {'customer_key': customer_key, 'profile_id': (latest or {}).get('profile_id', 'unpersisted-editor'),
                     'version': max(expected_version, 1), 'roles': deepcopy(roles)}
        # An edit is explicit confirmation of current anonymous literals. Alias
        # resolution for later drawings remains an injected compatibility input.
        aliases = deepcopy(self._aliases) or {}
        for role in roles.values():
            if isinstance(role, dict) and isinstance(role.get('block'), str) and role['block'].startswith('*'):
                aliases[role['block']] = role['block']
        checked = check_compatibility(deepcopy(self._intake), candidate, customer_key=customer_key, block_aliases=aliases)
        if checked['decision'] != 'apply':
            raise ValueError('edited roles are incompatible: ' + checked['reason'])
        try:
            return self.workflow.store.save_version(customer_key, checked['roles'], expected_version=expected_version)
        except ValueError as error:
            if str(error) == 'profile version changed':
                raise AdapterVersionConflict(str(error)) from error
            raise

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
