import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PhotoMaterialIntake from './PhotoMaterialIntake.jsx'
import { listProjectPhotos, readProjectPhotoPreview, uploadProjectPhoto } from '../projects/api.js'

const authority = vi.hoisted(() => ({ scope: 'account-a', listeners: new Set() }))
vi.mock('../auth/activeScope.js', () => ({
  readActiveScope: () => authority.scope,
  subscribeActiveScope: fn => { authority.listeners.add(fn); return () => authority.listeners.delete(fn) },
}))
vi.mock('../projects/api.js', () => ({ listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn(), uploadProjectPhoto: vi.fn() }))
const row = (name, id = name) => ({ material_id: id, name, media_type: 'image/jpeg' })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const choose = (...names) => fireEvent.change(screen.getByLabelText('Photo files'), { target: { files: names.map(name => new File(['image'], name, { type: 'image/jpeg' })) } })

beforeEach(() => {
  vi.clearAllMocks()
  authority.scope = 'account-a'
  listProjectPhotos.mockResolvedValue({ materials: [] })
  readProjectPhotoPreview.mockResolvedValue(new Blob(['preview'], { type: 'image/jpeg' }))
  URL.createObjectURL = vi.fn(() => 'blob:photo-preview')
  URL.revokeObjectURL = vi.fn()
})
afterEach(cleanup)

describe('project photo intake', () => {
  it('uploads sequentially, retains failures and retries only failed files', async () => {
    const first = deferred()
    uploadProjectPhoto.mockImplementationOnce(() => first.promise)
      .mockRejectedValueOnce(new Error('Upload failed. Try again.'))
      .mockResolvedValueOnce({ material: row('bread.jpg') })
    render(<PhotoMaterialIntake projectId="project-a" />)
    await screen.findByText('0 photos in this project.')
    choose('cake.heic', 'bread.jpg')
    expect(uploadProjectPhoto).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Add photos' }).disabled).toBe(true)
    await act(async () => first.resolve({ material: row('cake.heic') }))
    await screen.findByRole('button', { name: 'Retry failed photos' })
    expect(screen.getByText('1 photo ready. 1 need a retry.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed photos' }))
    await screen.findByText('2 photos ready.')
    expect(uploadProjectPhoto.mock.calls.map(call => call[1].name)).toEqual(['cake.heic', 'bread.jpg', 'bread.jpg'])
  })

  it('drops stale uploads and stops the queue when the project changes', async () => {
    const pending = deferred()
    uploadProjectPhoto.mockReturnValue(pending.promise)
    const { rerender } = render(<PhotoMaterialIntake projectId="project-a" />)
    choose('private.jpg', 'second.jpg')
    const signal = uploadProjectPhoto.mock.calls[0][2].signal
    rerender(<PhotoMaterialIntake projectId="project-b" />)
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve({ material: row('private.jpg') }))
    expect(screen.queryByText('private.jpg')).toBeNull()
    expect(uploadProjectPhoto).toHaveBeenCalledTimes(1)
  })

  it('revokes preview URLs and clears photos on an account change', async () => {
    listProjectPhotos.mockResolvedValueOnce({ materials: [row('cake.jpg')] })
    render(<PhotoMaterialIntake projectId="project-a" />)
    await screen.findByAltText('cake.jpg')
    await act(async () => { authority.scope = 'account-b'; for (const fn of authority.listeners) fn() })
    expect(screen.queryByAltText('cake.jpg')).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:photo-preview')
    expect(listProjectPhotos.mock.calls.at(-1)[1].expectedScope).toBe('account-b')
  })

  it('loads the next page without losing earlier photos', async () => {
    listProjectPhotos.mockResolvedValueOnce({ materials: [row('cake.jpg')], next_cursor: 'next-page' })
      .mockResolvedValueOnce({ materials: [row('bread.jpg')] })
    render(<PhotoMaterialIntake projectId="project-a" />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load more photos' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Load more photos' }))
    await screen.findByAltText('bread.jpg')
    expect(screen.getByAltText('cake.jpg')).toBeTruthy()
    expect(listProjectPhotos.mock.calls[1][1].cursor).toBe('next-page')
  })

  it('does not request or retain photos when signed out', () => {
    authority.scope = null
    render(<PhotoMaterialIntake projectId="project-a" />)
    expect(screen.getByText('Sign in and open a project to add photos.')).toBeTruthy()
    expect(listProjectPhotos).not.toHaveBeenCalled()
  })
})
