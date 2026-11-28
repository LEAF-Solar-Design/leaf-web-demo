import { afterEach, expect, it, vi } from 'vitest'
import { saveEditedDrawingVersion } from './api.js'

afterEach(() => vi.unstubAllGlobals())

it('uploads the edited version under the drawing file name', async () => {
  const fetchImpl = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ new_version: { version: 2 } }), { status: 200 },
  ))
  vi.stubGlobal('fetch', fetchImpl)
  await saveEditedDrawingVersion('d1', new Uint8Array([1]), 1, 'digest', null, { fileName: 'plant-room.dxf' })
  const body = fetchImpl.mock.calls[0][1].body
  expect(body.get('file').name).toBe('plant-room.dxf')
})
