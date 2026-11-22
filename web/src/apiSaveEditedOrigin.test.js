import { afterEach, expect, it, vi } from 'vitest'
import { saveEditedDrawingVersion } from './api.js'

afterEach(() => vi.unstubAllGlobals())

it.each([
  [undefined, 'edited.dxf', null],
  [{ origin: 'studio', fileName: '' }, 'edited.dxf', null],
  [{ origin: 'autocad', fileName: 'C:\\projects\\roof-site.dwg' }, 'roof-site.dxf', 'autocad'],
  [{ origin: 'autocad', fileName: '/tmp/roof.plan.dwg' }, 'roof.plan.dxf', 'autocad'],
  [{ fileName: 'roof?.dwg' }, 'edited.dxf', null],
  [{ fileName: '.dwg' }, 'edited.dxf', null],
  [{ fileName: 'a'.repeat(200) + '.dwg' }, 'a'.repeat(116) + '.dxf', null],
])('uploads the DXF with the requested name and origin for %o', async (opts, name, origin) => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetchMock)
  await saveEditedDrawingVersion('roof-1', new Uint8Array([1, 2]), 4, 'digest', 'cap', opts)
  const [, { body, headers }] = fetchMock.mock.calls[0]
  expect(body.get('file').name).toBe(name)
  expect(body.get('file').type).toBe('application/dxf')
  expect(body.get('file').size).toBe(2)
  expect(body.get('origin')).toBe(origin)
  expect(body.get('parent_version')).toBe('4')
  expect(body.get('source_digest')).toBe('digest')
  expect(headers['X-Checkout-Capability']).toBe('cap')
})
