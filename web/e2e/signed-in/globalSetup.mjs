import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { harnessNeedsBuild } from '../regressions/runGate.mjs'

export default async function globalSetup() {
  if (!await harnessNeedsBuild()) return
  const harness = fileURLToPath(new URL('../../../harness/', import.meta.url))
  const result = spawnSync(process.execPath, [resolve(harness, 'node_modules/typescript/bin/tsc'), '-p', 'tsconfig.build.json'], {
    cwd: harness, shell: false, windowsHide: true, stdio: 'inherit', timeout: 240_000,
  })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`Signed-in suite harness compilation failed (exit ${result.status}, signal ${result.signal})`)
}
