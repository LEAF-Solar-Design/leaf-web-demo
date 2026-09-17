import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import useMaterialIntake from './useMaterialIntake.js'
import useDrawingUploadController from '../controllers/upload/useDrawingUploadController.js'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  importUploadedDrawingVersion: vi.fn(),
  getGuestUploadPolicy: vi.fn(), uploadDrawing: vi.fn(),
  getDrawingUploadStatus: vi.fn(), getUploadedDrawingIntake: vi.fn(),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('reads the live upload snapshot before React renders the busy state', async () => {
  api.getGuestUploadPolicy.mockResolvedValue({ enabled: true, accepted: ['.dwg'], max_bytes: 100 })
  let finish
  api.uploadDrawing.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  api.getUploadedDrawingIntake.mockResolvedValue({ drawing: {} })
  api.importUploadedDrawingVersion.mockResolvedValue({ drawingVersion: { version: 1 }, replayed: false })
  const { result } = renderHook(() => {
    const upload = useDrawingUploadController()
    const intake = useMaterialIntake({ upload })
    return { upload, intake }
  })
  await waitFor(() => expect(result.current.upload.policy?.enabled).toBe(true))
  await act(async () => {
    expect(result.current.intake.begin({ projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' })).toBe(true)
    const pending = result.current.upload.actions.upload(new File(['dwg'], 'site.dwg'))
    expect(result.current.intake.begin({ projectId: 'p2', projectName: 'Roof B', fileName: 'other.dwg' })).toBe(false)
    finish({ drawing_id: 'u-0123456789', status: 'ready', extracted_version: 1 })
    await pending
  })
  expect(api.importUploadedDrawingVersion.mock.calls[0][0]).toBe('p1')
  expect(result.current.intake.target.fileName).toBe('site.dwg')
})

it('B3 row7 composes the existing ready callback and notifies attachment once', async () => {
  const drawingVersion = { version: 1, name: 'site.dwg' }
  api.getGuestUploadPolicy.mockResolvedValue({ enabled: true, accepted: ['.dwg'], max_bytes: 100 })
  api.uploadDrawing.mockResolvedValue({ drawing_id: 'u-0123456789', status: 'ready', extracted_version: 1 })
  api.getUploadedDrawingIntake.mockResolvedValue({ drawing: {} })
  api.importUploadedDrawingVersion.mockResolvedValue({ drawingVersion, replayed: false })
  const original = vi.fn()
  const additional = vi.fn()
  const onAttached = vi.fn()
  const { result, rerender } = renderHook(() => {
    const upload = useDrawingUploadController({ onReady: original })
    const intake = useMaterialIntake({ upload, onReady: additional, onAttached })
    return { upload, intake }
  })
  await waitFor(() => expect(result.current.upload.policy?.enabled).toBe(true))
  await act(async () => {
    result.current.intake.begin({ projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' })
    await result.current.upload.actions.upload(new File(['dwg'], 'site.dwg'))
  })
  expect(original).toHaveBeenCalledTimes(1)
  expect(additional).toHaveBeenCalledWith(original.mock.calls[0][0])
  expect(result.current.intake.phase).toBe('attached')
  expect(onAttached).toHaveBeenCalledExactlyOnceWith(drawingVersion)
  rerender()
  expect(onAttached).toHaveBeenCalledTimes(1)
})
