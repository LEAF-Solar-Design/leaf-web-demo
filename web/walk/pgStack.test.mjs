import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { test } from 'node:test'
import { createStackDatabase, dropStaleStackDatabases, PostgresUnavailableError, queryPostgres, stackAdminUrl, validateStackDatabaseName } from './pgStack.mjs'

const adminUrl = stackAdminUrl(process.env.LEAF_WALK_PG_ADMIN_URL || process.env.LEAF_GATE_DATABASE_URL || 'postgresql://leaf@127.0.0.1:25432/postgres')

async function exists(name) {
  return (await queryPostgres(adminUrl, 'SELECT 1 FROM pg_database WHERE datname = %s', [name])).length !== 0
}

test('unreachable authority reports a typed error with a redacted driver cause', { timeout: 60000 }, async () => {
  const url = 'postgresql://leaf:walk%40secret@127.0.0.1:1/postgres'
  const unavailable = (error) => {
    assert.ok(error instanceof PostgresUnavailableError)
    assert.equal(error.code, 'postgres_unavailable')
    assert.ok(error.cause instanceof Error)
    assert.ok(error.cause.message.length > 0)
    for (const secret of [url, 'walk%40secret', 'walk@secret']) assert.equal(error.cause.message.includes(secret), false)
    return true
  }
  await assert.rejects(dropStaleStackDatabases({ adminUrl: url }), unavailable)
  await assert.rejects(createStackDatabase({ adminUrl: url, slot: 0 }), unavailable)
})

test('create, connect, forced drop and repeated drop use a private database', { timeout: 120000 }, async () => {
  const database = await createStackDatabase({ adminUrl, slot: 0 })
  try {
    assert.match(database.name, /^leaf_walk_0_[a-z0-9]+_[0-9a-f]{12}$/)
    assert.ok(database.name.length <= 63)
    assert.equal(new URL(database.url).pathname, '/' + database.name)
    assert.deepEqual(await queryPostgres(database.url, 'SELECT current_database() AS name'), [{ name: database.name }])
    assert.equal(await exists(database.name), true)
    await Promise.all([database.drop(), database.drop()])
    assert.equal(await exists(database.name), false)
    await database.drop()
    assert.equal(await exists(database.name), false)
  } finally { await database.drop() }
})

test('database identifiers and slots refuse invalid names before connection', async () => {
  for (const name of ['', 'UPPER', 'a'.repeat(64), 'with-dash', 'x"; DROP DATABASE postgres; --', null]) {
    assert.throws(() => validateStackDatabaseName(name), /Database name must match/)
  }
  assert.equal(validateStackDatabaseName('a'.repeat(63)), 'a'.repeat(63))
  for (const slot of [-1, 475, 0.5, '1', undefined]) {
    await assert.rejects(createStackDatabase({ adminUrl: 'postgresql://unreachable.invalid/postgres', slot }), /slot must be an integer/)
  }
  await assert.rejects(dropStaleStackDatabases({ adminUrl, olderThanMinutes: -1 }), /olderThanMinutes/)
})

test('admin authority uses the walk override then the CI gate URL, never ambient DATABASE_URL', () => {
  const previous = { walk: process.env.LEAF_WALK_PG_ADMIN_URL, gate: process.env.LEAF_GATE_DATABASE_URL, database: process.env.DATABASE_URL }
  try {
    process.env.DATABASE_URL = 'postgresql://ambient.invalid/shared'
    process.env.LEAF_GATE_DATABASE_URL = 'postgresql://gate.invalid/gate'
    process.env.LEAF_WALK_PG_ADMIN_URL = 'postgresql://walk.invalid/admin'
    assert.equal(stackAdminUrl(), 'postgresql://walk.invalid/admin')
    delete process.env.LEAF_WALK_PG_ADMIN_URL
    assert.equal(stackAdminUrl(), 'postgresql://gate.invalid/gate')
    delete process.env.LEAF_GATE_DATABASE_URL
    assert.throws(() => stackAdminUrl(), /PostgreSQL admin URL is required/)
  } finally {
    for (const [key, value] of [['LEAF_WALK_PG_ADMIN_URL', previous.walk], ['LEAF_GATE_DATABASE_URL', previous.gate], ['DATABASE_URL', previous.database]]) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
})

test('stale sweep removes a marked stale database and preserves a fresh database', { timeout: 120000 }, async () => {
  const databases = []
  try {
    const stale = await createStackDatabase({ adminUrl, slot: 1 })
    databases.push(stale)
    const fresh = await createStackDatabase({ adminUrl, slot: 2 })
    databases.push(fresh)
    const marker = 'leaf-walk-created-at:' + (Date.now() - 121 * 60000)
    // Both literals are validated/generated locally; COMMENT does not accept
    // bind parameters for an identifier or comment string.
    await queryPostgres(adminUrl, `COMMENT ON DATABASE "${validateStackDatabaseName(stale.name)}" IS '${marker}'`)
    const dropped = await dropStaleStackDatabases({ adminUrl })
    assert.ok(dropped.includes(stale.name), JSON.stringify(dropped))
    assert.equal(dropped.includes(fresh.name), false)
    assert.ok(dropped.length <= 50)
    assert.equal(await exists(stale.name), false)
    assert.equal(await exists(fresh.name), true)
    assert.deepEqual(await queryPostgres(fresh.url, 'SELECT current_database() AS name'), [{ name: fresh.name }])
  } finally { await Promise.all(databases.map((database) => database.drop())) }
})

test('two slots have distinct databases and cannot read each other\'s rows', { timeout: 120000 }, async () => {
  const databases = []
  try {
    const a = await createStackDatabase({ adminUrl, slot: 3 })
    databases.push(a)
    const b = await createStackDatabase({ adminUrl, slot: 4 })
    databases.push(b)
    assert.notEqual(a.name, b.name)
    assert.notEqual(a.url, b.url)
    await queryPostgres(a.url, 'CREATE TABLE walk_private_probe (value text)')
    await queryPostgres(a.url, 'INSERT INTO walk_private_probe VALUES (%s)', ['only on A'])
    assert.deepEqual(await queryPostgres(a.url, 'SELECT value FROM walk_private_probe'), [{ value: 'only on A' }])
    assert.deepEqual(await queryPostgres(b.url, "SELECT to_regclass('public.walk_private_probe') AS relation"), [{ relation: null }])
  } finally { await Promise.all(databases.map((database) => database.drop())) }
})

test('stale sweep recovers a database left without its creation comment', { timeout: 120000 }, async () => {
  const now = Date.now
  const createdAt = now() - 121 * 60000
  let database
  try {
    // A crash between CREATE and COMMENT leaves only the timestamp in its
    // name. The Python sweeper uses the server-side process clock.
    try {
      Date.now = () => createdAt
      database = await createStackDatabase({ adminUrl, slot: 6 })
    } finally { Date.now = now }
    await queryPostgres(adminUrl, `COMMENT ON DATABASE "${validateStackDatabaseName(database.name)}" IS NULL`)
    const dropped = await dropStaleStackDatabases({ adminUrl })
    assert.ok(dropped.includes(database.name), JSON.stringify(dropped))
    assert.equal(await exists(database.name), false)
  } finally {
    Date.now = now
    if (database) await database.drop()
  }
})

test('process exit drops its database even without an explicit drop call', { timeout: 120000 }, async () => {
  const source = `import { createStackDatabase } from ${JSON.stringify(new URL('./pgStack.mjs', import.meta.url).href)};
    const database = await createStackDatabase({ slot: 5 });
    console.log(database.name);
    process.exit(0);`
  const child = spawn(process.execPath, ['--input-type=module', '-e', source], {
    windowsHide: true, env: { ...process.env, LEAF_WALK_PG_ADMIN_URL: adminUrl }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let output = ''
  let diagnostic = ''
  child.stdout.on('data', (chunk) => { output += chunk })
  child.stderr.on('data', (chunk) => { diagnostic += chunk })
  const timer = setTimeout(() => child.kill('SIGKILL'), 90000)
  try {
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
    assert.equal(code, 0, diagnostic)
    const name = output.trim()
    assert.match(name, /^leaf_walk_5_[a-z0-9]+_[0-9a-f]{12}$/)
    assert.equal(await exists(name), false, 'exit cleanup must finish before the child exits')
  } finally { clearTimeout(timer) }
})
