import { writeFileSync } from "node:fs";
import pg from "pg";

const { Pool } = pg;
const url = process.env.TEST_DATABASE_URL;
const tableName = process.env.TEST_LEASE_TABLE;
const tenantId = process.env.TEST_TENANT_ID;
const dirtyPath = process.env.TEST_DIRTY_PATH;
if (!url || !tableName || !tenantId || !/^[a-z_][a-z0-9_]*$/i.test(tableName)) {
  process.exit(2);
}
const table = `"${tableName}"`;
const token = crypto.randomUUID();
const pool = new Pool({ connectionString: url, max: 2 });

async function acquire() {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [tenantId]);
    await client.query(
      `INSERT INTO ${table}
         (tenant_id, owner_token, generation, acquired_at, heartbeat_at, expires_at)
       VALUES ($1, $2, 1, clock_timestamp(), clock_timestamp(),
               clock_timestamp() + interval '300 milliseconds')
       ON CONFLICT (tenant_id) DO UPDATE
         SET owner_token = EXCLUDED.owner_token,
             generation = ${table}.generation + 1,
             acquired_at = clock_timestamp(),
             heartbeat_at = clock_timestamp(),
             expires_at = clock_timestamp() + interval '300 milliseconds'
       WHERE ${table}.expires_at <= clock_timestamp()`,
      [tenantId, token],
    );
    await client.query("COMMIT");
  } finally {
    client.release();
  }
}

await acquire();
if (dirtyPath) writeFileSync(dirtyPath, "partial author edit\n", "utf8");
process.stdout.write("READY\n");
setInterval(async () => {
  await pool.query(
    `UPDATE ${table}
        SET heartbeat_at = clock_timestamp(),
            expires_at = clock_timestamp() + interval '300 milliseconds'
      WHERE tenant_id = $1 AND owner_token = $2`,
    [tenantId, token],
  );
}, 75);
