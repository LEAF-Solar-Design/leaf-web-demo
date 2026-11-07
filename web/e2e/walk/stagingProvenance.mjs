import { execFile } from 'node:child_process'
import { deliveryRepo } from './stagingDelivery.mjs'

function runLocalGit(args, options) {
  return new Promise(resolve => {
    execFile('git', args, options, (error, stdout) => resolve({ code: error ? error.code : 0, stdout }))
  })
}

export async function readHarnessCommit({ run = runLocalGit, now = () => new Date() } = {}) {
  let read_at = new Date().toISOString()
  try {
    read_at = now().toISOString()
    const result = await run(['rev-parse', 'HEAD'], { cwd: deliveryRepo, shell: false, timeout: 2000, maxBuffer: 4096 })
    const sha = typeof result?.stdout === 'string' ? result.stdout.trim() : ''
    if (result?.code === 0 && /^[0-9a-f]{40}$/i.test(sha)) return { sha, read_at, reason: 'ok' }
  } catch { /* Missing Git, timeouts and invalid reads are diagnostic only. */ }
  return { sha: null, read_at, reason: 'unavailable' }
}

export function walkProvenance(delivery, harness) {
  const deployed = component => ({
    sha: component.source_sha ?? null,
    read_at: component.observation === 'not_configured' ? null : delivery.checkedAt,
    reason: component.observation,
  })
  return { schema: 'leaf.walk-provenance.v1', harness_commit: harness,
    deployed_app_sha: deployed(delivery.app), deployed_web_sha: deployed(delivery.web) }
}
