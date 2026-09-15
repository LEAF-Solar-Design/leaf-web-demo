import { afterEach, describe, expect, it, vi } from 'vitest'
import { nlPrompt } from './api.js'

const tools = [{ name: 'count-by-layer', description: 'Count entities by layer' }]

afterEach(() => { vi.unstubAllGlobals() })

describe('nlPrompt demo and service failure states', () => {
  it('labels demo matching without calling the service', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    expect(await nlPrompt(true, 'Something unusual', tools)).toMatchObject({ tool: null, stub: true, stubKind: 'demo' })
    expect(await nlPrompt(true, 'Count entities', tools)).toMatchObject({ tool: 'count-by-layer', stubKind: 'demo' })
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['Something unusual', '/unknown', '12.5, -34.7'])('keeps live failure distinct and refuses %s', async (text) => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Connection lost')))
    const route = await nlPrompt(false, text, tools)
    expect(route).toMatchObject({ tool: null, stub: true, stubKind: 'outage' })
    expect(route.stubReason).toEqual(expect.any(String))
    expect(route.stubReason.length).toBeGreaterThan(0)
  })

  it('marks even supported fallback suggestions as an outage', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Connection lost')))
    expect(await nlPrompt(false, 'Count entities', tools)).toMatchObject({
      tool: 'count-by-layer', stub: true, stubKind: 'outage',
    })
  })

  it('preserves successful live routes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ lane: 'run', tool: 'count-by-layer', confidence: 0.9 }),
    }))
    expect(await nlPrompt(false, 'Count entities', tools)).toMatchObject({
      tool: 'count-by-layer', stub: false, alternatives: [],
    })
  })

  it('rethrows unauthorized responses instead of falling back', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }))
    await expect(nlPrompt(false, 'Count entities', tools)).rejects.toMatchObject({ status: 401 })
  })
})
