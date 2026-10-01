import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const databases = new Set()
let exitCleanupInstalled = false

export class PostgresUnavailableError extends Error {
  constructor(cause) {
    super('PostgreSQL server is unavailable', { cause })
    this.name = 'PostgresUnavailableError'
    this.code = 'postgres_unavailable'
  }
}

export function stackAdminUrl(adminUrl) {
  const value = adminUrl ?? process.env.LEAF_WALK_PG_ADMIN_URL ?? process.env.LEAF_GATE_DATABASE_URL
  if (!value) throw new Error('PostgreSQL admin URL is required: set LEAF_WALK_PG_ADMIN_URL or LEAF_GATE_DATABASE_URL')
  let parsed
  try { parsed = new URL(value) } catch { throw new TypeError('PostgreSQL admin URL is invalid') }
  if (!['postgres:', 'postgresql:'].includes(parsed.protocol) || !parsed.hostname || !parsed.pathname.slice(1)) throw new TypeError('PostgreSQL admin URL must include a host and database')
  return parsed.href
}

export function validateStackDatabaseName(name) {
  if (typeof name !== 'string' || !/^[a-z0-9_]{1,63}$/.test(name)) throw new TypeError('Database name must match ^[a-z0-9_]{1,63}$')
  return name
}

// psycopg is already required by the platform. Keep credentials in stdin,
// never argv, and use libpq's autocommit for CREATE/DROP DATABASE.
const helper = String.raw`
import json, re, sys, time
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

request = json.load(sys.stdin)
action = request['action']
name = request.get('name')
if name is not None and not re.fullmatch(r'[a-z0-9_]{1,63}', name):
    raise ValueError('invalid database name')
if action == 'migrate':
    import os
    os.environ['DATABASE_URL'] = request['url']
    import platform_link
    db = platform_link.platform_db()
    try:
        db.apply_migration()
        result = db.assert_schema_current()
    finally:
        db.reset_pool()
else:
    try:
        connection = psycopg.connect(request['url'], autocommit=True, connect_timeout=5,
                                     row_factory=dict_row)
    except psycopg.Error as error:
        print(json.dumps({'error': 'postgres_unavailable', 'message': str(error)}))
        sys.exit(2)
    with connection as conn:
        conn.execute("SET statement_timeout = '15s'")
        if action == 'create':
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            conn.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(name), sql.Literal('leaf-walk-created-at:' + str(request['createdAt']))))
            result = None
        elif action == 'drop':
            conn.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
            result = None
        elif action == 'sweep':
            rows = conn.execute("SELECT datname AS name, shobj_description(oid, 'pg_database') AS marker "
                                "FROM pg_database WHERE left(datname, 10) = 'leaf_walk_' "
                                "AND datname <> current_database() ORDER BY datname").fetchall()
            cutoff = time.time() * 1000 - request['olderThanMinutes'] * 60000
            stale = []
            for row in rows:
                marker = re.fullmatch(r'leaf-walk-created-at:([0-9]+)', row['marker'] or '')
                embedded = re.fullmatch(r'leaf_walk_[0-9]+_([a-z0-9]+)_[0-9a-f]{12}', row['name'])
                created = int(marker[1]) if marker else int(embedded[1], 36) if embedded else None
                if (created is not None and created < cutoff and
                        re.fullmatch(r'[a-z0-9_]{1,63}', row['name'])):
                    stale.append((created, row['name']))
            result = []
            for _, stale_name in sorted(stale)[:50]:
                conn.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(stale_name)))
                result.append(stale_name)
        elif action == 'query':
            cursor = conn.execute(request['statement'], request.get('params', []))
            result = cursor.fetchall() if cursor.description else []
        else:
            raise ValueError('unknown PostgreSQL operation')
print(json.dumps(result, default=str))
`

function helperOptions() {
  return { cwd: resolve(repo, 'server'), windowsHide: true,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' } }
}

function redactDiagnostic(message, url) {
  let safe = message.split(url).join('[database URL]')
  const password = new URL(url).password
  const secrets = new Set([password])
  try { secrets.add(decodeURIComponent(password)) } catch {}
  for (const secret of secrets) {
    if (secret) safe = safe.split(secret).join('[redacted]')
  }
  return safe.trim()
}

async function execute(request, timeoutMs = 30000) {
  const python = process.env.LEAF_TEST_PYTHON || process.env.PYTHON || 'python'
  const child = spawn(python, ['-B', '-c', helper], { ...helperOptions(), stdio: ['pipe', 'pipe', 'pipe'] })
  let output = ''
  let diagnostic = ''
  let timedOut = false
  child.stdout.on('data', (chunk) => { output += chunk })
  child.stderr.on('data', (chunk) => { diagnostic = (diagnostic + chunk).slice(-8000) })
  // A missing interpreter or an early import error may close stdin first.
  child.stdin.on('error', () => {})
  child.stdin.end(JSON.stringify(request))
  const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL') }, timeoutMs)
  try {
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
    if (timedOut || code !== 0) {
      let failure
      try { failure = JSON.parse(output) } catch {}
      if (failure?.error === 'postgres_unavailable') {
        throw new PostgresUnavailableError(new Error(redactDiagnostic(failure.message, request.url)))
      }
      const safe = redactDiagnostic(diagnostic, request.url)
      throw new Error(`PostgreSQL ${request.action} ${timedOut ? 'timed out' : `failed (${code})`}: ${safe}`)
    }
    return JSON.parse(output)
  } finally { clearTimeout(timer) }
}

function dropOnExit(record) {
  const python = process.env.LEAF_TEST_PYTHON || process.env.PYTHON || 'python'
  const result = spawnSync(python, ['-B', '-c', helper], { ...helperOptions(),
    input: JSON.stringify({ action: 'drop', url: record.adminUrl, name: record.name }),
    encoding: 'utf8', timeout: 30000, stdio: ['pipe', 'pipe', 'pipe'] })
  if (result.error || result.status !== 0) process.exitCode = process.exitCode || 1
}

export async function createStackDatabase({ adminUrl, slot } = {}) {
  if (!Number.isInteger(slot) || slot < 0 || slot > 474) throw new RangeError('slot must be an integer from 0 through 474')
  adminUrl = stackAdminUrl(adminUrl)
  const createdAt = Date.now()
  const name = validateStackDatabaseName(`leaf_walk_${slot}_${createdAt.toString(36)}_${randomBytes(6).toString('hex')}`)
  const target = new URL(adminUrl)
  target.pathname = '/' + name
  target.searchParams.delete('dbname')
  const record = { adminUrl, name }
  // Register before CREATE so even a crash between CREATE and COMMENT gets
  // synchronous cleanup; the embedded timestamp also lets the next run sweep it.
  databases.add(record)
  if (!exitCleanupInstalled) {
    exitCleanupInstalled = true
    process.once('exit', () => { for (const database of databases) dropOnExit(database) })
  }
  let dropping
  let dropped = false
  const drop = () => {
    if (dropped) return Promise.resolve()
    if (!dropping) dropping = execute({ action: 'drop', url: adminUrl, name }).then(() => {
      dropped = true
      databases.delete(record)
    }).finally(() => { dropping = undefined })
    return dropping
  }
  try { await execute({ action: 'create', url: adminUrl, name, createdAt }) } catch (error) {
    // A failed connection cannot have created a database. Preserve its typed
    // error rather than obscuring it with another failed cleanup connection.
    if (error instanceof PostgresUnavailableError) { databases.delete(record); throw error }
    try { await drop() } catch (cleanupError) { throw new AggregateError([error, cleanupError], 'PostgreSQL creation and cleanup failed') }
    throw error
  }
  return { url: target.href, name, drop }
}

export async function dropStaleStackDatabases({ adminUrl, olderThanMinutes = 120 } = {}) {
  if (!Number.isFinite(olderThanMinutes) || olderThanMinutes < 0) throw new RangeError('olderThanMinutes must be a nonnegative number')
  return execute({ action: 'sweep', url: stackAdminUrl(adminUrl), olderThanMinutes }, 900000)
}

export async function prepareStackDatabase(url) {
  return execute({ action: 'migrate', url }, 180000)
}

// The fixture proofs use the same driver to inspect catalog state and seed a
// drawing version, as the canonical server walkthrough does.
export async function queryPostgres(url, statement, params = []) {
  return execute({ action: 'query', url, statement, params })
}
