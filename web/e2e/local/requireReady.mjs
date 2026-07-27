import { setTimeout as delay } from 'node:timers/promises'

export async function requireLocalReady(request, test, apiBase) {
  let lastStatus = 'no response'
  let lastReport = null
  for (let attempt = 0; attempt < 12; attempt += 1) {
    try {
      const response = await request.get(`${apiBase}/api/ready`, { timeout: 3_000 })
      lastStatus = String(response.status())
      lastReport = await response.json().catch(() => null)
      if (response.ok() && lastReport?.ready) return lastReport
    } catch (error) {
      lastStatus = error instanceof Error ? error.message : String(error)
    }
    await delay(500)
  }

  const dependencies = lastReport?.dependencies
    ? JSON.stringify(lastReport.dependencies)
    : 'no dependency report'
  const reason = `real local stack is not ready at ${apiBase}; last status ${lastStatus}; ${dependencies}`
  if (process.env.LEAF_E2E_MANAGED === '1') throw new Error(reason)
  test.skip(true, reason)
}
