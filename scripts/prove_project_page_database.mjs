// Focused local proof. Run through the allocated offhost test runner.
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { startEphemeralPostgres } from '../web/walk/pgEphemeral.mjs'

const root = fileURLToPath(new URL('../', import.meta.url))
const postgres = await startEphemeralPostgres({ slot: 0 })
try {
  const status = await new Promise((resolve, reject) => {
    const child = spawn(process.argv[2], ['-P', '-m', 'pytest', '-q',
      'platform/tests/test_project_web_page.py',
      'platform/tests/test_db_readiness_static.py',
      'platform/tests/test_db_schema_proof_static.py'], {
      cwd: root, stdio: 'inherit', timeout: 240000,
      env: { ...process.env, DATABASE_URL: postgres.adminUrl },
    })
    child.once('error', reject)
    child.once('close', resolve)
  })
  if (status !== 0) throw new Error(`Page database proof failed: ${status}`)
} finally {
  await postgres.stop()
}
