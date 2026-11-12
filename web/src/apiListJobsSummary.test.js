import { afterEach, expect, it, vi } from 'vitest'

import { listJobs } from './api.js'

afterEach(() => {
  vi.unstubAllGlobals()
})

it('listJobs requests summaries and returns the jobs array unchanged', async () => {
  const jobs = [{ job_id: 'j1', status: 'complete', cost: { usd_est: 0.125 } }]
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ jobs }),
  }))
  vi.stubGlobal('fetch', fetchImpl)

  expect(await listJobs()).toBe(jobs)
  expect(fetchImpl).toHaveBeenCalledTimes(1)
  expect(fetchImpl.mock.calls[0][0]).toMatch(/\/api\/jobs\?limit=20&summary=1$/)
})
