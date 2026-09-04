// W4g-1c: the public demo's head as the static sample DXF (api.js
// fetchSampleDxf): the same answer shape as fetchDrawingDxf, the same byte
// ceiling, a typed error on any refusal, and no request to the API at all.
import { describe, expect, it, vi } from 'vitest'

import { MAX_DRAWING_DXF_BYTES, SAMPLE_DXF_PATH, fetchSampleDxf } from './api.js'

function response({ status = 200, bytes = new Uint8Array([48, 10, 69, 79, 70, 10]), contentLength = null } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name.toLowerCase() === 'content-length' ? (contentLength == null ? null : String(contentLength)) : null) },
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  }
}

describe('fetchSampleDxf', () => {
  it('answers the opener the way the DXF route does, from the static asset, at version 1', async () => {
    const fetchImpl = vi.fn(async () => response())
    const answer = await fetchSampleDxf('rooftop_demo', { fetchImpl })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(SAMPLE_DXF_PATH)
    expect(answer.bytes).toBeInstanceOf(Uint8Array)
    expect(Array.from(answer.bytes)).toEqual([48, 10, 69, 79, 70, 10])
    expect(answer).toMatchObject({ version: 1, head: 1, source: 'sample-static', etag: null, drawingId: 'rooftop_demo' })
  })

  it('refuses a missing asset with the status, and an oversized one before a byte is read', async () => {
    await expect(fetchSampleDxf('rooftop_demo', { fetchImpl: async () => response({ status: 404 }) }))
      .rejects.toMatchObject({ status: 404, message: `GET ${SAMPLE_DXF_PATH} -> 404` })
    const arrayBuffer = vi.fn()
    const big = { ...response({ contentLength: MAX_DRAWING_DXF_BYTES + 1 }), arrayBuffer }
    await expect(fetchSampleDxf('rooftop_demo', { fetchImpl: async () => big })).rejects.toMatchObject({ status: 413 })
    expect(arrayBuffer).not.toHaveBeenCalled()
  })
})
