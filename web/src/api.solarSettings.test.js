import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCapabilities, runToolAsync } from './api.js'

const I = { schema_version: 1, source_intake_sha256: 'a'.repeat(64), units: { drawing_units: 'ft', wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], elevation_datum: 'unknown', crs: null } }
const P = { expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: I }
const error = { error_code: 'bad_params', message: 'invalid_seed_parent', retryable: false }

function stubResponse(body, status = 200) {
  const fetch = vi.fn().mockResolvedValue({ ok: status >= 200 && status < 300, status, json: async () => body })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const pathAndQuery = (url) => {
  const parsed = new URL(url, 'https://example.test')
  return parsed.pathname + parsed.search
}

afterEach(() => vi.unstubAllGlobals())

describe('Solar settings API transport', () => {
  it('SF2 row20a the settings 409 keeps its reason_code', async () => {
    const fetch = stubResponse({ error, reason_code: 'invalid_seed_parent', degraded_mode: false }, 409)
    const envelope = await runToolAsync({ name: 'solar-settings' }, P, 'd1', {})
    expect(envelope).toMatchObject({ ok: false, tool: 'solar-settings', reason_code: 'invalid_seed_parent', error })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('SF2 row23 getCapabilities sends the drawing context', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false, { drawing_id: 'd1', drawing_version: 3 })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(pathAndQuery(fetch.mock.calls[0][0])).toBe('/api/capabilities?drawing_id=d1&drawing_version=3')
  })

  it('SF2 row24 getCapabilities without a drawing sends the bare path', async () => {
    const fetch = stubResponse({ families: [] })
    await getCapabilities(false)
    await getCapabilities(false, { drawing_id: '' })
    expect(fetch).toHaveBeenCalledTimes(2)
    for (const [url] of fetch.mock.calls) expect(pathAndQuery(url)).toBe('/api/capabilities')
  })

  it('SF2 row27 another tool 409 keeps the old envelope', async () => {
    stubResponse({ error, reason_code: 'invalid_seed_parent', degraded_mode: false }, 409)
    const envelope = await runToolAsync({ name: 'solar-size-strings' }, P, 'd1', {})
    expect(envelope).toEqual({ ok: false, tool: 'solar-size-strings', version: null, result: null, overlay: null,
      timing_ms: 0, cost: null, error, degraded_mode: false })
    expect(Object.hasOwn(envelope, 'reason_code')).toBe(false)
  })

  it('SF2 row28 a malformed reason_code is dropped', async () => {
    for (const reason_code of ['x'.repeat(65), 'bad code!', 7]) {
      stubResponse({ error, reason_code, degraded_mode: false }, 409)
      const envelope = await runToolAsync({ name: 'solar-settings' }, P, 'd1', {})
      expect(Object.hasOwn(envelope, 'reason_code')).toBe(false)
      expect(envelope.error).toEqual(error)
    }
  })
})
