import { afterEach, expect, it, vi } from 'vitest'
import { config, importUploadedDrawingVersion, setStoredOrgId } from './api.js'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear() })
const source = { drawingId: 'u-0123456789', version: 2, name: 'site.dwg' }
const options = { idempotencyKey: 'b3:p1:u-0123456789:2' }

it('B3 row13 distinguishes HTTP errors from invalid successful responses', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response('Service unavailable', { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'This key was already used.' }), { status: 409 }))
    .mockResolvedValueOnce(new Response('not json', { status: 201 })))
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'HTTP 503', status: 503 })
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'This key was already used.', status: 409 })
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'invalid_response', status: 201 })
})

it('B3 row6 sends the import contract and preserves success and server errors', async () => {
  localStorage.setItem('leaf.jwt', 'test-token')
  setStoredOrgId('org-test')
  const drawingVersion = { version: 2, name: 'site.dwg' }
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ drawing_version: drawingVersion, replayed: false }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'This key was already used.' }), { status: 409 }))
  vi.stubGlobal('fetch', fetch)
  await expect(importUploadedDrawingVersion('p1', source, options)).resolves.toEqual({ drawingVersion, replayed: false })
  const [url, init] = fetch.mock.calls[0]
  expect(url).toBe(`${config.apiBase}/api/projects/p1/drawing-versions/import`)
  expect(init.method).toBe('POST')
  expect(init.headers).toMatchObject({ 'Idempotency-Key': options.idempotencyKey, 'X-Tenant-Id': config.tenant, 'X-Org-Id': 'org-test', Authorization: 'Bearer test-token', 'Content-Type': 'application/json' })
  expect(JSON.parse(init.body)).toEqual({ source: { drawing_id: source.drawingId, version: 2 }, name: 'site.dwg' })
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'This key was already used.', status: 409 })
})

it('rejects malformed and oversized response bodies', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('not json', { status: 201 }))
    .mockResolvedValueOnce(new Response('x'.repeat(1024 * 1024 + 1), { status: 201 })))
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'invalid_response' })
  await expect(importUploadedDrawingVersion('p1', source, options)).rejects.toMatchObject({ message: 'invalid_response' })
})

it('bounds even a stalled response body to thirty seconds', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 201, headers: new Headers(), text: () => new Promise(() => {}) }))
  const request = expect(importUploadedDrawingVersion('p1', source, options)).rejects.toThrow('30000 ms')
  await vi.advanceTimersByTimeAsync(30000)
  await request
})
