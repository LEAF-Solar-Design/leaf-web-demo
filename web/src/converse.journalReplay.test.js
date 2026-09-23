import { afterEach, expect, it, vi } from 'vitest'
vi.mock('./api.js', () => ({ config: { apiBase: '', tenant: 'T' }, authHeaders: () => ({}), noteUnauthorized: vi.fn() }))
vi.mock('./telemetry.js', () => ({ trackErrorShown: vi.fn(), trackStreamDown: vi.fn() }))
import { postMessage } from './converse.js'

afterEach(() => vi.unstubAllGlobals())

function reply(status, body) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status, ok: status >= 200 && status < 300, json: async () => body }))
}

const completed = { request_id: 'r1', turn_id: 't1', status: 'completed', stop_reason: 'end_turn' }

it('a. accepts the stored completed answer for the journaled request', async () => {
  reply(200, completed)
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).resolves.toEqual(completed)
})

it('b. rejects a completed answer when no request identity was sent', async () => {
  reply(200, completed)
  await expect(postMessage('S', { text: 'move' })).rejects.toMatchObject({ status: 200 })
})

it('b2. rejects a completed answer that names no request when none was sent', async () => {
  reply(200, { turn_id: 't1', status: 'completed', stop_reason: 'end_turn' })
  await expect(postMessage('S', { text: 'move' })).rejects.toMatchObject({ status: 200 })
})

it('c. rejects a completed answer for a different request', async () => {
  reply(200, { ...completed, request_id: 'r2' })
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 200 })
})

it('d. rejects a failed answer even with HTTP 200', async () => {
  reply(200, { ...completed, status: 'failed' })
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 200 })
})

it('e. rejects completed answers with missing or empty turn identities', async () => {
  reply(200, { request_id: 'r1', status: 'completed' })
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 200 })
  reply(200, { ...completed, turn_id: '' })
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 200 })
})

it('e2. rejects a completed answer carried by a 2xx other than 200', async () => {
  reply(201, completed)
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 201 })
})

it('f. preserves the tagged status and error code of a failed journaled answer', async () => {
  reply(503, { request_id: 'r1', turn_id: 't1', status: 'failed', error: { error_code: 'BROKER_UNREACHABLE', message: 'down' } })
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).rejects.toMatchObject({ status: 503, errorCode: 'BROKER_UNREACHABLE' })
})

it('g. accepts started answers with and without a request identity', async () => {
  const started = { turn_id: 't1', status: 'started' }
  reply(202, started)
  await expect(postMessage('S', { text: 'move', request_id: 'r1' })).resolves.toEqual(started)
  await expect(postMessage('S', { text: 'move' })).resolves.toEqual(started)
})
