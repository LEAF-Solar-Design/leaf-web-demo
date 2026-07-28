import { expect, test } from '@playwright/test'

import { createPlatformTrustController } from '../../../src/controllers/platform/createPlatformTrustController.js'
import {
  classifyHealth,
  entitlementAllowed,
  resolveQuotaStatus,
} from '../../../src/controllers/platform/platformTrustModel.js'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, reject, resolve }
}

test('mock mode performs no platform or grant requests', async () => {
  const calls = []
  const service = (name) => async () => { calls.push(name); return {} }
  const controller = createPlatformTrustController({
    mock: true,
    services: {
      getUsage: service('usage'),
      getEntitlements: service('entitlements'),
      getHealth: service('health'),
      getClaudeGrant: service('grant'),
      linkClaudeGrant: service('link'),
      unlinkClaudeGrant: service('unlink'),
    },
  })

  await controller.refreshAll()
  await controller.loadUsage()
  await controller.loadGrant()
  await controller.linkClaude('must-not-leave-the-test', 'oauth')
  await controller.unlinkClaude()

  expect(calls).toEqual([])
  expect(controller.getSnapshot()).toMatchObject({
    mock: true,
    usage: null,
    entitlements: null,
    health: null,
    grant: null,
    usageLoading: false,
    entLoading: false,
    healthLoading: false,
    grantLoading: false,
    grantBusy: false,
  })
})

test('only the latest overlapping usage request may publish', async () => {
  const first = deferred()
  const second = deferred()
  let call = 0
  let clock = 100
  const controller = createPlatformTrustController({
    services: { getUsage: () => (++call === 1 ? first.promise : second.promise) },
    now: () => ++clock,
  })

  const older = controller.loadUsage()
  const newer = controller.loadUsage()
  expect(controller.getSnapshot().usageLoading).toBe(true)

  second.resolve({ today: { runs: 2 }, cap: { enabled: true, remaining: 5 } })
  await newer
  const acceptedAt = controller.getSnapshot().usageAt
  expect(controller.getSnapshot()).toMatchObject({ usage: { today: { runs: 2 } }, usageLoading: false })

  first.resolve({ today: { runs: 99 }, cap: { enabled: true, remaining: 0 } })
  await older
  expect(controller.getSnapshot().usage.today.runs).toBe(2)
  expect(controller.getSnapshot().usageAt).toBe(acceptedAt)
})

test('switching to mock invalidates an in-flight live read', async () => {
  const pending = deferred()
  const controller = createPlatformTrustController({
    services: { getHealth: () => pending.promise },
  })

  const reading = controller.loadHealth()
  expect(controller.getSnapshot().healthLoading).toBe(true)
  controller.setMock(true)
  pending.resolve({ ok: true, da_client_present: true })
  await reading

  expect(controller.getSnapshot()).toMatchObject({
    mock: true,
    health: null,
    healthLoading: false,
    authRequired: false,
  })
})

test('loading and grant failures remain explicit without retaining the token', async () => {
  const grantRead = deferred()
  const controller = createPlatformTrustController({
    services: {
      getClaudeGrant: () => grantRead.promise,
      linkClaudeGrant: async () => { throw new Error('The supplied Claude credential expired.') },
      unlinkClaudeGrant: async () => { throw new Error('Could not unlink this Claude account.') },
    },
    formatError: (error) => error.message,
  })

  const reading = controller.loadGrant()
  expect(controller.getSnapshot().grantLoading).toBe(true)
  grantRead.resolve({ linked: false, linked_at: null })
  await reading
  expect(controller.getSnapshot()).toMatchObject({ grantLoading: false, grant: { linked: false } })

  const token = 'sensitive-one-time-token'
  await controller.linkClaude(token, 'oauth')
  expect(controller.getSnapshot()).toMatchObject({
    grantBusy: false,
    grant: { linked: false },
    grantErr: 'The supplied Claude credential expired.',
  })
  expect(JSON.stringify(controller.getSnapshot())).not.toContain(token)

  await controller.unlinkClaude()
  expect(controller.getSnapshot().grantErr).toBe('Could not unlink this Claude account.')
})

test('grant mutations preserve the token-free mounted account pool', async () => {
  const calls = []
  const accountOne = { id: 'acct-1', label: 'Team east', kind: 'oauth', plan: 'team', eligible: true, active: true }
  const accountTwo = { id: 'acct-2', label: 'Enterprise west', kind: 'oauth', plan: 'enterprise', eligible: true, active: false }
  const pool = (active, accounts = [accountOne, accountTwo]) => ({
    linked: accounts.length > 0,
    linked_at: accounts[0]?.linked_at || '2026-07-27T12:00:00Z',
    kind: accounts.find((account) => account.id === active)?.kind || null,
    active_account_id: active,
    accounts: accounts.map((account) => ({ ...account, active: account.id === active })),
  })
  const controller = createPlatformTrustController({
    services: {
      linkClaudeGrant: async (...args) => { calls.push(['link', ...args]); return pool('acct-2') },
      activateClaudeGrant: async (id) => { calls.push(['activate', id]); return pool(id) },
      unlinkClaudeGrant: async (id) => { calls.push(['unlink', id]); return pool('acct-2', [accountTwo]) },
    },
  })
  const token = 'sensitive-team-setup-token'

  await controller.linkClaude(token, 'oauth', 'Enterprise west', 'enterprise')
  expect(controller.getSnapshot().grant.accounts).toHaveLength(2)
  await controller.activateClaude('acct-1')
  expect(controller.getSnapshot().grant.active_account_id).toBe('acct-1')
  await controller.unlinkClaude('acct-1')
  expect(controller.getSnapshot().grant.accounts.map((account) => account.id)).toEqual(['acct-2'])
  expect(calls).toEqual([
    ['link', token, 'oauth', 'Enterprise west', 'enterprise'],
    ['activate', 'acct-1'],
    ['unlink', 'acct-1'],
  ])
  expect(JSON.stringify(controller.getSnapshot())).not.toContain(token)
})

test('auth sources coordinate and a successful retry clears only its source', async () => {
  let failUsage = true
  const authEvents = []
  const controller = createPlatformTrustController({
    services: {
      getUsage: async () => {
        if (failUsage) { const error = new Error('GET /api/usage -> 401'); error.status = 401; throw error }
        return { today: { runs: 1 } }
      },
    },
    onAuthRequired: (required, sources) => authEvents.push({ required, sources }),
  })

  controller.reportAuthRequired(true, 'jobs')
  await controller.loadUsage()
  expect(controller.getSnapshot()).toMatchObject({ authRequired: true })
  expect(controller.getSnapshot().authSources).toEqual(['jobs', 'platform-trust:usage'])

  failUsage = false
  await controller.loadUsage()
  expect(controller.getSnapshot().authSources).toEqual(['jobs'])
  controller.reportAuthRequired(false, 'jobs')
  expect(controller.getSnapshot()).toMatchObject({ authRequired: false, authSources: [] })
  expect(authEvents.at(-1)).toMatchObject({ required: false, sources: [] })
})

test('quota clearing requires a strictly fresher successful usage read', () => {
  const spend = {
    ok: false,
    error: { error_code: 'quota_exceeded', message: 'Spend cap reached.' },
  }
  const daily = {
    ok: false,
    quota_kind: 'daily_runs',
    limit: 10,
    used: 10,
    error: { error_code: 'quota_exceeded', message: 'Daily limit reached.' },
  }

  expect(resolveQuotaStatus({
    result: spend,
    conditionAt: 50,
    usageAt: 50,
    usage: { cap: { enabled: true, remaining: 5 } },
  })).toMatchObject({ cleared: false, visible: { kind: 'spend' }, freshUsage: null })
  expect(resolveQuotaStatus({
    result: spend,
    conditionAt: 50,
    usageAt: 51,
    usage: { cap: { enabled: true, remaining: 5 } },
  })).toMatchObject({ cleared: true, visible: null })
  expect(resolveQuotaStatus({
    result: daily,
    conditionAt: 50,
    usageAt: 51,
    usage: { today: { runs: 9 } },
  })).toMatchObject({ cleared: true, condition: { kind: 'daily_runs', limit: 10 } })
})

test('unknown policy stays permissive while explicit denials and degraded health stay honest', () => {
  expect(entitlementAllowed(null, 'build')).toBe(true)
  expect(entitlementAllowed({ entitlements: { build: false } }, 'build')).toBe(false)
  expect(entitlementAllowed({ entitlements: { build: true } }, 'build')).toBe(true)
  expect(classifyHealth(null)).toEqual({ status: 'unknown', degraded: false })
  expect(classifyHealth({ ok: false })).toEqual({ status: 'degraded', degraded: true })
  expect(classifyHealth({ ok: true })).toEqual({ status: 'healthy', degraded: false })
})

test('controller restarts after a framework lifecycle cleanup', async () => {
  let healthReads = 0
  const controller = createPlatformTrustController({
    services: {
      getHealth: async () => {
        healthReads += 1
        return { ok: true }
      },
    },
  })

  controller.destroy()
  controller.start()
  await controller.loadHealth()

  expect(healthReads).toBe(1)
  expect(controller.getSnapshot().health).toEqual({ ok: true })
})
