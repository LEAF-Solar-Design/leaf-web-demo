import { afterEach, describe, expect, it, vi } from 'vitest'
import { editedUploadName, saveDrawingVersionPlan, saveEditedDrawingVersion } from './api.js'

afterEach(() => vi.unstubAllGlobals())

function stubFetch() {
  const fetchImpl = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ new_version: { version: 2 } }), { status: 200 },
  ))
  vi.stubGlobal('fetch', fetchImpl)
  return fetchImpl
}

describe('editedUploadName', () => {
  it.each([
    ['plant-room.dxf', 'plant-room.dxf'],
    ['Parking-Site-R2.dwg', 'Parking-Site-R2.dxf'],
    ['C:\Demo\Parking-Site-R2.dwg', 'Parking-Site-R2.dxf'],
  ])('keeps the drawing stem of %j', (input, expected) => {
    expect(editedUploadName(input)).toBe(expected)
  })

  it.each([undefined, null, '', '.dxf', 'con.dxf', 'a|b.dxf', 'x\ny.dxf'])('falls back to edited.dxf for %j', input => {
    expect(editedUploadName(input)).toBe('edited.dxf')
  })
})

it('uploads the edited version under the drawing file name', async () => {
  const fetchImpl = stubFetch()
  await saveEditedDrawingVersion('d1', new Uint8Array([1]), 1, 'digest', null, { fileName: 'plant-room.dxf' })
  expect(fetchImpl.mock.calls[0][1].body.get('file').name).toBe('plant-room.dxf')
})

it('a plan save uploads under the drawing file name too, not edited.dxf', async () => {
  const fetchImpl = stubFetch()
  await saveDrawingVersionPlan('d2', new Uint8Array([1]), 1, 'digest', {}, null, { fileName: 'Parking-Site-R2.dwg' })
  const call = fetchImpl.mock.calls.find(([url]) => String(url).includes('/versions/plan'))
  expect(call[1].body.get('file').name).toBe('Parking-Site-R2.dxf')
})

it('a plan save without a file name keeps edited.dxf', async () => {
  const fetchImpl = stubFetch()
  await saveDrawingVersionPlan('d3', new Uint8Array([1]), 1, 'digest', {}, null, {})
  const call = fetchImpl.mock.calls.find(([url]) => String(url).includes('/versions/plan'))
  expect(call[1].body.get('file').name).toBe('edited.dxf')
})
