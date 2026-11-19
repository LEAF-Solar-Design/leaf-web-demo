import { afterEach, describe, expect, it, vi } from 'vitest'
import { safeEditedFileName, saveEditedDrawingVersion } from './api.js'

afterEach(() => vi.unstubAllGlobals())

describe('safeEditedFileName', () => {
  it.each(['plant-room.dxf', 'roof.DXF', 'a'.repeat(120) + '.dxf'])('accepts %j', name => {
    expect(safeEditedFileName(name)).toBe(name)
  })

  it.each(['a.dwg', '../a.dxf', 'a\\b.dxf', '', 12, 'a'.repeat(121) + '.dxf',
    'x"y.dxf', 'x\ny.dxf'])('falls back for %j', name => {
    expect(safeEditedFileName(name)).toBe('edited.dxf')
  })
})

it('uploads the edited version under the supplied filename', async () => {
  const fetchImpl = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ new_version: { version: 2 } }), { status: 200 },
  ))
  vi.stubGlobal('fetch', fetchImpl)
  await saveEditedDrawingVersion('d1', new Uint8Array([1]), 1, 'digest', null, 'plant-room.dxf')
  const body = fetchImpl.mock.calls[0][1].body
  expect(body.get('file').name).toBe('plant-room.dxf')
})
