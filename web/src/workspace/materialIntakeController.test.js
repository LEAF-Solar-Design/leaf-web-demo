import { expect, it, vi } from 'vitest'
import { createMaterialIntakeController } from './createMaterialIntakeController.js'

const target = { projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' }
const ready = { receipt: { drawing_id: 'u-0123456789' }, status: { status: 'ready', extracted_version: 2 } }
const drawingVersion = { drawing_id: 'u-0123456789', version: 2, name: 'site.dwg' }
const setup = () => {
  const importUpload = vi.fn().mockResolvedValue({ drawingVersion, replayed: false })
  return { importUpload, controller: createMaterialIntakeController({ services: { importUpload } }) }
}

it('B3 row1 attaches the extracted version and original filename', async () => {
  const { controller, importUpload } = setup()
  controller.begin(target)
  await controller.onUploadReady(ready)
  expect(importUpload).toHaveBeenCalledExactlyOnceWith('p1', { drawingId: 'u-0123456789', version: 2, name: 'site.dwg' }, { idempotencyKey: 'b3:p1:u-0123456789:2' })
  expect(controller.getSnapshot()).toMatchObject({ phase: 'attached', drawing: drawingVersion, attached: true })
})

it('B3 row2 keeps the captured project through a caller project switch', async () => {
  const { controller, importUpload } = setup()
  const current = { ...target }
  controller.begin(current)
  current.projectId = 'p2'
  current.projectName = 'Roof B'
  await controller.onUploadReady(ready)
  expect(importUpload.mock.calls[0][0]).toBe('p1')
  expect(controller.getSnapshot().target).toEqual(target)
})

it('B3 row3 leaves a failed extraction pending without importing and resets', () => {
  const { controller, importUpload } = setup()
  controller.begin(target)
  // Extraction failure never emits the upload controller ready callback.
  expect(controller.getSnapshot().phase).toBe('pending')
  expect(importUpload).not.toHaveBeenCalled()
  controller.reset()
  expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', target: null })
})

it('B3 row4 retries an unavailable import with its original key', async () => {
  const { controller, importUpload } = setup()
  importUpload.mockRejectedValueOnce(Object.assign(new Error('Import is temporarily disabled.'), { status: 503 }))
    .mockResolvedValueOnce({ drawingVersion, replayed: true })
  controller.begin(target)
  await controller.onUploadReady(ready)
  expect(controller.getSnapshot()).toMatchObject({ phase: 'attach-failed', error: 'Import is temporarily disabled.' })
  await controller.retry()
  expect(importUpload.mock.calls[1]).toEqual(importUpload.mock.calls[0])
  expect(controller.getSnapshot()).toMatchObject({ phase: 'attached', replayed: true })
})

it('B3 row5 ignores unclaimed, duplicate and reset ready events', async () => {
  const { controller, importUpload } = setup()
  await controller.onUploadReady(ready)
  expect(importUpload).not.toHaveBeenCalled()
  let finish
  importUpload.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  controller.begin(target)
  const first = controller.onUploadReady(ready)
  const second = controller.onUploadReady(ready)
  expect(first).toBe(second)
  await Promise.resolve()
  finish({ drawingVersion, replayed: false })
  await first
  await controller.onUploadReady(ready)
  expect(importUpload).toHaveBeenCalledTimes(1)
  controller.reset()
  await controller.onUploadReady(ready)
  expect(importUpload).toHaveBeenCalledTimes(1)
})

it('does not publish an attachment after reset', async () => {
  const { controller, importUpload } = setup()
  let finish
  importUpload.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  controller.begin(target)
  const pending = controller.onUploadReady(ready)
  await Promise.resolve()
  controller.reset()
  finish({ drawingVersion, replayed: false })
  await pending
  expect(controller.getSnapshot().phase).toBe('idle')
})
