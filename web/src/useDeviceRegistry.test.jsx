// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import useDeviceRegistry, { deviceError, DeviceRequestError } from './useDeviceRegistry.js'

vi.mock('./api.js', () => ({
  authHeaders: () => ({ Authorization: 'Bearer owner-test' }),
  config: { apiBase: '' }, noteUnauthorized: (response) => response,
}))

const device = { device_id: 'd1', project_id: 'p1', label: 'Mac mini', packages: [] }
const catalogEntry = { package: 'homeassistant', display_name: 'Home Assistant', version: '2026.10.9', port: 8123, health_path: '/api/', relay_allow: [] }
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ devices: [device] }))))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('scopes the list to the project, authenticates, and strips unknown and nested secret fields', async () => {
  fetch.mockResolvedValue(json({ devices: [{ ...device, token: 'secret', public_key_private: 'private', ha_token: 'ha-secret', platform: { os: 'darwin', token: 'secret' }, packages: [{ package: 'homeassistant', version: 'v1', status: 'running', ha_token: 'secret' }] }] }))
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p 1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  expect(fetch).toHaveBeenCalledWith('/api/devices?project_id=p%201', expect.objectContaining({ headers: { Authorization: 'Bearer owner-test' }, cache: 'no-store' }))
  expect(JSON.stringify(result.current.devices)).not.toMatch(/secret|private|ha_token|token/)
})

it('makes no network calls in mock mode or without a project, including mutations', async () => {
  const { result, rerender } = renderHook(({ mock, projectId }) => useDeviceRegistry({ mock, projectId }), { initialProps: { mock: true, projectId: 'p1' } })
  await act(async () => {
    expect(await result.current.enroll('Mac')).toBeNull()
    expect(await result.current.command('d1', 'ha.onboard', {})).toBeNull()
    expect(await result.current.relayToken('d1', 'Ops')).toBeNull()
    expect(await result.current.revoke('d1')).toBeNull()
    expect(await result.current.audit('d1')).toBeNull()
    expect(await result.current.loadCatalog()).toEqual([{ package: 'homeassistant', display_name: 'Home Assistant', version: '2026.10.4' }])
  })
  rerender({ mock: false, projectId: null })
  expect(result.current.devices).toEqual([])
  await act(async () => { expect(await result.current.loadCatalog()).toBeNull() })
  expect(fetch).not.toHaveBeenCalled()
})

it('reads the authenticated catalog lazily and projects only the fields needed by the drawer', async () => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  expect(result.current.catalog).toEqual([])
  expect(fetch).toHaveBeenCalledTimes(1)
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  let pending
  act(() => { pending = result.current.loadCatalog() })
  expect(result.current.catalogLoading).toBe(true)
  await act(async () => {
    settle(json({ packages: [{ ...catalogEntry, token: 'catalog-secret', ha_token: 'catalog-private' }] }))
    await pending
  })
  expect(fetch).toHaveBeenLastCalledWith('/api/devices/packages', expect.objectContaining({ method: 'GET', headers: { Authorization: 'Bearer owner-test' }, cache: 'no-store' }))
  expect(result.current.catalog).toEqual([{ package: 'homeassistant', display_name: 'Home Assistant', version: '2026.10.9' }])
  expect(result.current.catalogLoading).toBe(false)
  expect(result.current.catalogError).toBeNull()
  expect(JSON.stringify(result.current.catalog)).not.toMatch(/secret|private|token/)
})

it('clears a previously loaded catalog on failure, humanizes the error, and allows retry', async () => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockResolvedValueOnce(json({ packages: [catalogEntry] }))
  await act(async () => { await result.current.loadCatalog() })
  expect(result.current.catalog).toHaveLength(1)
  fetch.mockResolvedValueOnce(json({ error: { code: 'service_unavailable', message: 'sensitive catalog internals' } }, 503))
  await act(async () => { expect(await result.current.loadCatalog()).toBeNull() })
  expect(result.current.catalog).toEqual([])
  expect(result.current.catalogLoading).toBe(false)
  expect(result.current.catalogError).toBe('Device management is temporarily unavailable. Try again.')
  fetch.mockResolvedValueOnce(json({ packages: [catalogEntry] }))
  await act(async () => { await result.current.loadCatalog() })
  expect(result.current.catalog[0].version).toBe('2026.10.9')
  expect(result.current.catalogError).toBeNull()
})

it.each([{}, { packages: null }, { packages: [{ ...catalogEntry, version: '' }] }])('fails closed on a malformed catalog response: %j', async (body) => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockResolvedValueOnce(json(body))
  await act(async () => { expect(await result.current.loadCatalog()).toBeNull() })
  expect(result.current.catalog).toEqual([])
  expect(result.current.catalogError).toBe('The package catalog could not be loaded. Try again.')
  expect(result.current.catalogLoading).toBe(false)
})

it('drops a catalog response arriving after switching to guest and exposes only the fixed mock catalog', async () => {
  const { result, rerender } = renderHook(({ mock }) => useDeviceRegistry({ mock, projectId: 'p1' }), { initialProps: { mock: false } })
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  let pending
  act(() => { pending = result.current.loadCatalog() })
  rerender({ mock: true })
  await act(async () => { settle(json({ packages: [catalogEntry] })); expect(await pending).toBeNull() })
  expect(result.current.catalog).toEqual([{ package: 'homeassistant', display_name: 'Home Assistant', version: '2026.10.4' }])
  expect(result.current.catalogLoading).toBe(false)
  expect(result.current.catalogError).toBeNull()
  expect(fetch).toHaveBeenCalledTimes(2)
})

it('does not let an older project catalog replace the new scope', async () => {
  const { result, rerender } = renderHook(({ projectId }) => useDeviceRegistry({ projectId }), { initialProps: { projectId: 'p1' } })
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  let pending
  act(() => { pending = result.current.loadCatalog() })
  rerender({ projectId: 'p2' })
  expect(result.current.catalog).toEqual([])
  expect(result.current.catalogLoading).toBe(false)
  fetch.mockResolvedValueOnce(json({ packages: [{ ...catalogEntry, version: 'new-scope' }] }))
  await act(async () => { await result.current.loadCatalog() })
  await act(async () => { settle(json({ packages: [catalogEntry] })); expect(await pending).toBeNull() })
  expect(result.current.catalog[0].version).toBe('new-scope')
})

it('clears guest data and ignores a response arriving after the mode switch', async () => {
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  const { result, rerender } = renderHook(({ mock }) => useDeviceRegistry({ mock, projectId: 'p1' }), { initialProps: { mock: false } })
  rerender({ mock: true })
  await act(async () => settle(json({ devices: [device] })))
  expect(result.current.devices).toEqual([])
  expect(result.current.error).toBeNull()
  expect(result.current.loading).toBe(false)
})

it('ignores an older project list even when it arrives after a new project', async () => {
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  fetch.mockResolvedValue(json({ devices: [{ ...device, project_id: 'p2', label: 'New project' }] }))
  const { result, rerender } = renderHook(({ projectId }) => useDeviceRegistry({ projectId }), { initialProps: { projectId: 'p1' } })
  rerender({ projectId: 'p2' })
  await waitFor(() => expect(result.current.devices[0]?.label).toBe('New project'))
  await act(async () => settle(json({ devices: [device] })))
  expect(result.current.devices[0].label).toBe('New project')
})

it('preserves cached rows on failure and offers a retry with a safe error', async () => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockResolvedValueOnce(json({ error: { code: 'service_unavailable', message: 'secret credentials' } }, 503))
  await act(async () => { await result.current.refresh() })
  expect(result.current.devices).toHaveLength(1)
  expect(result.current.error).toMatchObject({ kind: 'list', message: 'Device management is temporarily unavailable. Try again.' })
  await act(async () => { await result.current.error.retry() })
  expect(result.current.error).toBeNull()
})

it('uses the exact enrollment, command, audit, revoke, and relay wire shapes', async () => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.loading).toBe(false))
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/enrollments')) return json({ enrollment_id: 'e1', token: 'do-not-retain', install_command: 'official installer', expires_at: '2026-11-02T12:00:00Z' }, 201)
    if (url.endsWith('/relay-tokens')) return json({ relay_token: 'one-use', relay_origin: 'https://example.test', path_prefix: '/api/device-relay/homeassistant', ha_token: 'secret' }, 201)
    if (url.includes('/audit')) return json({ events: [{ at: '2026-11-02T10:00:00Z', op: 'pkg.health', actor: 'device', target: 'd1', status: 'ok', token: 'hidden' }] })
    if (url.endsWith('/revoke')) return json({ revoked_at: '2026-11-02T10:00:00Z' })
    if (url.endsWith('/commands/c1')) return json({ command: {}, result: null })
    if (url.endsWith('/commands')) return json({ command_id: 'c1' }, 202)
    return json({ devices: [device] })
  })
  await act(async () => {
    expect(await result.current.enroll('Mac')).not.toHaveProperty('token')
    expect(await result.current.command('d1', 'pkg.health', { package: 'homeassistant' })).toEqual({ command_id: 'c1' })
    expect(await result.current.getCommand('d1', 'c1')).toEqual({ command: {}, result: null })
    expect((await result.current.audit('d1'))[0]).not.toHaveProperty('token')
    expect(await result.current.relayToken('d1', 'Ops')).not.toHaveProperty('ha_token')
    await result.current.revoke('d1')
  })
  const sent = (path) => fetch.mock.calls.find(([url]) => url.endsWith(path))?.[1]
  expect(JSON.parse(sent('/enrollments').body)).toEqual({ project_id: 'p1', label: 'Mac' })
  expect(JSON.parse(sent('/commands').body)).toEqual({ op: 'pkg.health', args: { package: 'homeassistant' } })
  expect(JSON.parse(sent('/relay-tokens').body)).toEqual({ package: 'homeassistant', label: 'Ops' })
  expect(sent('/revoke')).not.toHaveProperty('body')
  expect(fetch.mock.calls.some(([url]) => url === '/api/devices/d1/audit?limit=50')).toBe(true)
})

it.each([
  ['enroll', ['Mac']],
  ['command', ['d1', 'pkg.health', { package: 'homeassistant' }]],
  ['revoke', ['d1']],
  ['relayToken', ['d1', 'Ops']],
])('rejects %s refusals with typed status and code for both server body shapes', async (method, args) => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  for (const [body, code] of [
    [{ error: { code: 'forbidden', message: 'private owner gate diagnostics' } }, 'forbidden'],
    [{ detail: 'forbidden' }, 'forbidden'],
    [{ detail: 'tenant owner authority required' }, null],
  ]) {
    fetch.mockResolvedValueOnce(json(body, 403))
    await act(async () => {
      const pending = result.current[method](...args)
      await expect(pending).rejects.toBeInstanceOf(DeviceRequestError)
      await expect(pending).rejects.toMatchObject({ status: 403, code, message: 'Device request failed.' })
      await expect(pending).rejects.not.toHaveProperty('body')
    })
    expect(result.current.error).toBe('Only a project owner can link devices. Ask your workspace owner to make you an owner of this project.')
    expect(result.current.busy).toBe(false)
  }
  expect(fetch).toHaveBeenCalledTimes(4)
})

it.each([
  [422, { error: { code: 'version_unknown' } }, 'version_unknown', 'This version is not in the device catalog. Refresh the agent and retry.'],
  [422, { error: { code: 'path_denied' } }, 'path_denied', 'This action is not allowed by the device catalog.'],
  [422, { error: { code: 'package_unknown' } }, 'package_unknown', 'This package is not in the device catalog.'],
  [422, { error: { code: 'unsupported_op' } }, 'unsupported_op', 'This agent does not support that action. Update the agent and retry.'],
  [503, { error: { code: 'device_offline' } }, 'device_offline', 'This device is offline. Check its connection and retry.'],
  [503, { error: { code: 'service_unavailable' } }, 'service_unavailable', 'Device management is temporarily unavailable. Try again.'],
  [503, { error: { code: 'unknown_failure' } }, null, 'Device management is temporarily unavailable. Try again.'],
  [422, { error: { code: 'unknown_failure', message: 'private diagnostics' } }, null, "That didn't work. Try again."],
  [422, { error: { code: 'toString' } }, null, "That didn't work. Try again."],
  [422, { detail: 'version_unknown' }, 'version_unknown', 'This version is not in the device catalog. Refresh the agent and retry.'],
  [422, { detail: 'path_denied' }, 'path_denied', 'This action is not allowed by the device catalog.'],
  [422, { error: { code: 'unknown_failure' }, detail: 'path_denied' }, 'path_denied', 'This action is not allowed by the device catalog.'],
  [422, { detail: 'private diagnostics' }, null, "That didn't work. Try again."],
])('keeps only known command error codes for HTTP %s with body %j', async (status, body, code, message) => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockResolvedValueOnce(json(body, status))
  await act(async () => {
    const pending = result.current.command('d1', 'pkg.install', { package: 'homeassistant', version: 'catalog-v2' })
    await expect(pending).rejects.toMatchObject({ status, code, message: 'Device request failed.' })
    await expect(pending).rejects.not.toHaveProperty('body')
    await expect(pending).rejects.not.toHaveProperty('detail')
  })
  expect(result.current.error).toBe(message)
})

it.each([
  [401, 'Your session expired. Sign in again.'],
  [403, 'Only a project owner can link devices. Ask your workspace owner to make you an owner of this project.'],
  [429, 'Too many requests. Try again in a minute.'],
])('prioritizes HTTP %s guidance over a known device code', (status, message) => {
  expect(deviceError(new DeviceRequestError(status, 'device_offline'))).toBe(message)
})

it.each([undefined, '', 'unknown_failure', 'private diagnostics', 'toString', '__proto__', {}])('does not store an unrecognized error code: %j', (code) => {
  expect(new DeviceRequestError(422, code).code).toBeNull()
})

it('uses safe guidance for command result codes without an HTTP status', () => {
  expect(deviceError({ error: { code: 'path_denied' } })).toBe('This action is not allowed by the device catalog.')
  expect(deviceError({ error: { code: 'unknown_failure', message: 'private diagnostics' } })).toBe("That didn't work. Try again.")
})

it('deviceError maps code no_wiz_light to useful connection guidance', () => {
  const message = 'No WiZ light answered at that address. Check the light is powered on and the IP is right. On a Mac, also allow Python under System Settings > Privacy & Security > Local Network.'
  expect(deviceError({ code: 'no_wiz_light' })).toBe(message)
  expect(deviceError({ error: { code: 'no_wiz_light' } })).toBe(message)
  expect(deviceError(new DeviceRequestError(422, 'no_wiz_light'))).toBe(message)
})

it.each([401, 403, 429, 503, 500])('preserves status %s even when a refusal body is not JSON', async (status) => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockResolvedValueOnce({ ok: false, status, json: async () => { throw new SyntaxError('private proxy response') } })
  await act(async () => {
    await expect(result.current.enroll('Mac')).rejects.toMatchObject({ status, code: null, name: 'DeviceRequestError' })
  })
  expect(result.current.busy).toBe(false)
  expect(result.current.error).not.toContain('private')
})

it('types network failures safely and clears the mutation error on retry', async () => {
  const { result } = renderHook(() => useDeviceRegistry({ projectId: 'p1' }))
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  fetch.mockRejectedValueOnce(new TypeError('private connection diagnostics'))
  await act(async () => {
    await expect(result.current.enroll('Mac')).rejects.toMatchObject({ status: 0, code: null, name: 'DeviceRequestError' })
  })
  expect(result.current.error).toBe('Device management is temporarily unavailable. Try again.')
  expect(result.current.busy).toBe(false)
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  let pending
  act(() => { pending = result.current.enroll('Mac') })
  expect(result.current.error).toBeNull()
  expect(result.current.busy).toBe(true)
  await act(async () => {
    settle(json({ enrollment_id: 'e1', install_command: 'official installer', expires_at: '2026-11-02T12:00:00Z' }, 201))
    await pending
  })
  expect(result.current.error).toBeNull()
  expect(result.current.busy).toBe(false)
})

it('does not refresh or return a credential from a mutation completing after switching to guest', async () => {
  const { result, rerender } = renderHook(({ mock }) => useDeviceRegistry({ mock, projectId: 'p1' }), { initialProps: { mock: false } })
  await waitFor(() => expect(result.current.devices).toHaveLength(1))
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  let pending
  act(() => { pending = result.current.relayToken('d1', 'Ops') })
  rerender({ mock: true })
  await act(async () => { settle(json({ relay_token: 'late-secret' }, 201)); expect(await pending).toBeNull() })
  expect(result.current.devices).toEqual([])
  expect(result.current.busy).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(2)
})
