import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readActiveScope } from '../auth/activeScope.js'
import { listProjectPhotos, readProjectPhotoPreview, uploadProjectPhoto } from './api.js'
const project = '11111111-2222-4333-8444-555555555555'
const photo = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
const token = `e30.${btoa(JSON.stringify({ sub: 'photo-test', iss: 'test' }))}.proof`
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('leaf.jwt', token)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ materials: [] }), { headers: { 'Content-Type': 'application/json' } })))
})
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

it('sends original bytes and filename with current authentication', async () => {
  const file = new File(['original image bytes'], 'cake #1.heic', { type: 'image/heic' })
  await uploadProjectPhoto(project, file, { expectedScope: readActiveScope() })
  const [url, options] = fetch.mock.calls[0]
  expect(url).toContain('filename=cake%20%231.heic')
  expect(options.body).toBe(file)
  expect(options.headers.Authorization).toBe(`Bearer ${token}`)
  expect(options.headers['Content-Type']).toBe('image/heic')
  expect(options.expectedScope).toBeUndefined()
})
it('rejects an account change before transmitting any bytes', async () => {
  const scope = readActiveScope()
  localStorage.removeItem('leaf.jwt')
  await expect(uploadProjectPhoto(project, new File(['bytes'], 'cake.jpg'), { expectedScope: scope })).rejects.toThrow('account changed')
  expect(fetch).not.toHaveBeenCalled()
})
it('reads authenticated binary previews without exposing a bearer in the URL', async () => {
  fetch.mockResolvedValue(new Response('jpeg bytes', { headers: { 'Content-Type': 'image/jpeg' } }))
  const blob = await readProjectPhotoPreview(project, photo, { expectedScope: readActiveScope() })
  expect(blob.type).toBe('image/jpeg')
  expect(fetch.mock.calls[0][0]).not.toContain(token)
  expect(fetch.mock.calls[0][1].cache).toBe('no-store')
})
it('encodes pagination and refuses malformed project identity', async () => {
  await listProjectPhotos(project, { cursor: 'next/cursor' })
  expect(fetch.mock.calls[0][0]).toContain('cursor=next%2Fcursor')
  expect(() => listProjectPhotos('../foreign')).toThrow('not a valid id')
  expect(fetch).toHaveBeenCalledTimes(1)
})
