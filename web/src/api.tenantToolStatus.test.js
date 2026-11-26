import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let api

beforeEach(async () => {
  vi.resetModules()
  localStorage.clear()
  api = await import('./api.js')
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('returns null on 404 without telemetry requests or request failure diagnostics', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 404 }))
  vi.stubGlobal('fetch', fetch)

  await expect(api.getTenantToolStatus()).resolves.toBeNull()
  const { flushNow } = await import('./telemetry.js')
  flushNow()

  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/tool-envelopes\/tenant\/status$/)
  expect(fetch.mock.calls[0][1]).toMatchObject({ cache: 'no-store' })
  expect(api.recentRequestFailures()).toEqual([])
})

it('maps the three status fields from a successful response', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    workspace_id: 'workspace-1', workspace_name: 'Design', last_plugin_check_at: '2026-11-26T12:00:00Z',
    ignored: 'extra',
  }))))

  await expect(api.getTenantToolStatus()).resolves.toEqual({
    workspaceId: 'workspace-1', workspaceName: 'Design', lastPluginCheckAt: '2026-11-26T12:00:00Z',
  })
})

it('maps missing and non-string fields to null', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    workspace_id: 42, workspace_name: false,
  }))))

  await expect(api.getTenantToolStatus()).resolves.toEqual({
    workspaceId: null, workspaceName: null, lastPluginCheckAt: null,
  })
})

it.each([null, [], 'status', 42, true])('returns null for a non-object body: %j', async body => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))

  await expect(api.getTenantToolStatus()).resolves.toBeNull()
})

it('returns null when fetch throws', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

  await expect(api.getTenantToolStatus()).resolves.toBeNull()
  expect(api.recentRequestFailures()).toEqual([])
})

it('preserves sign-in invalidation on 401 without request failure diagnostics', async () => {
  localStorage.setItem('leaf.jwt', 'current-token')
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }))
  vi.stubGlobal('fetch', fetch)

  await expect(api.getTenantToolStatus()).resolves.toBeNull()

  expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer current-token')
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(api.recentRequestFailures()).toEqual([])
})

it('makes no request in mock mode', async () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  await api.getSessionIdentity(true)

  await expect(api.getTenantToolStatus()).resolves.toBeNull()

  expect(fetch).not.toHaveBeenCalled()
})
