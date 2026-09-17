import { expect, it, vi } from 'vitest'
import { createMaterialIntakeController } from './createMaterialIntakeController.js'
import { createDrawingUploadController } from '../controllers/upload/createDrawingUploadController.js'

const target = { projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' }
const ready = { receipt: { drawing_id: 'u-0123456789' }, status: { status: 'ready', extracted_version: 2 } }
const drawingVersion = { drawing_id: 'u-0123456789', version: 2, name: 'site.dwg' }
const setup = () => {
  const importUpload = vi.fn().mockResolvedValue({ drawingVersion, replayed: false })
  return { importUpload, controller: createMaterialIntakeController({ services: { importUpload } }) }
}
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
const uploadServices = () => ({
  policy: vi.fn().mockResolvedValue({ enabled: true }),
  upload: vi.fn().mockResolvedValue({ ...ready.receipt, ...ready.status }),
  status: vi.fn().mockResolvedValue(ready.status),
  intake: vi.fn().mockResolvedValue({ drawing: {} }),
  wait: vi.fn().mockResolvedValue(),
})

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

it('B3 row3 leaves a failed extraction pending without importing and resets', async () => {
  const { controller, importUpload } = setup()
  const services = uploadServices()
  services.upload.mockResolvedValue({ ...ready.receipt, status: 'extracting' })
  services.status.mockResolvedValue({ status: 'failed', error: 'Extraction failed.' })
  const upload = createDrawingUploadController({ services })
  const workspace = { drawing: { name: 'Existing drawing' } }
  const original = workspace.drawing
  upload.subscribeReady(async (result) => {
    await controller.onUploadReady(result)
    workspace.drawing = result.view.drawing
  })
  await upload.loadPolicy()
  controller.begin(target)
  await upload.upload({ name: 'site.dwg', size: 1 })
  expect(services.status).toHaveBeenCalledTimes(1)
  expect(upload.getSnapshot()).toMatchObject({ phase: 'failed', error: 'Extraction failed.' })
  expect(workspace.drawing).toBe(original)
  expect(controller.getSnapshot().phase).toBe('pending')
  expect(importUpload).not.toHaveBeenCalled()
  controller.reset()
  expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', target: null })
})

it('B3 row10 refuses to replace the target until the upload settles', async () => {
  const services = uploadServices()
  const receipt = deferred()
  services.upload.mockReturnValueOnce(receipt.promise)
  const upload = createDrawingUploadController({ services })
  const importUpload = vi.fn().mockResolvedValue({ drawingVersion, replayed: false })
  const controller = createMaterialIntakeController({ services: { importUpload }, isUploadInFlight: () => {
    const snapshot = upload.getSnapshot()
    return snapshot.busy || ['uploading', 'extracting', 'loading'].includes(snapshot.phase)
  } })
  upload.subscribeReady(controller.onUploadReady)
  await upload.loadPolicy()
  expect(controller.begin(target)).toBe(true)
  const pending = upload.upload({ name: target.fileName, size: 1 })
  const next = { projectId: 'p2', projectName: 'Roof B', fileName: 'other.dwg' }
  expect(controller.begin(next)).toBe(false)
  expect(controller.getSnapshot()).toMatchObject({ phase: 'pending', target, beginRefused: 'Wait for the current upload to finish.' })
  receipt.resolve({ ...ready.receipt, ...ready.status })
  await pending
  expect(importUpload).toHaveBeenCalledExactlyOnceWith('p1', { drawingId: ready.receipt.drawing_id, version: 2, name: target.fileName }, { idempotencyKey: 'b3:p1:u-0123456789:2' })
  expect(controller.begin(next)).toBe(true)
  expect(controller.getSnapshot()).toMatchObject({ target: next, beginRefused: null })
  controller.reset()
  expect(controller.getSnapshot().beginRefused).toBeNull()
})

it('B3 row11 prevents a cancelled attachment from publishing over the next upload', async () => {
  const services = uploadServices()
  const importResult = deferred()
  const importStarted = deferred()
  const secondReceipt = deferred()
  const importUpload = vi.fn(() => { importStarted.resolve(); return importResult.promise })
  const controller = createMaterialIntakeController({ services: { importUpload } })
  const upload = createDrawingUploadController({ services })
  upload.subscribeReady(controller.onUploadReady)
  await upload.loadPolicy()
  controller.begin(target)
  const first = upload.upload({ name: target.fileName, size: 1 })
  await importStarted.promise
  upload.cancel()
  services.upload.mockReturnValueOnce(secondReceipt.promise)
  const second = upload.upload({ name: 'second.dwg', size: 1 })
  const snapshot = upload.getSnapshot()
  expect(snapshot).toMatchObject({ busy: true, phase: 'uploading' })
  importResult.resolve({ drawingVersion, replayed: false })
  expect(await first).toBeNull()
  expect(upload.getSnapshot()).toBe(snapshot)
  upload.cancel()
  secondReceipt.resolve({ ...ready.receipt, ...ready.status })
  await second
})

it('B3 row12 keeps single flight across begin and reset and retries failed keys', async () => {
  const { controller, importUpload } = setup()
  const result = deferred()
  importUpload.mockReturnValueOnce(result.promise)
  controller.begin(target)
  const first = controller.onUploadReady(ready)
  controller.begin(target)
  const second = controller.onUploadReady(ready)
  expect(second).toBe(first)
  await Promise.resolve()
  expect(importUpload).toHaveBeenCalledTimes(1)
  result.resolve({ drawingVersion, replayed: false })
  await first
  controller.reset()
  controller.begin(target)
  await controller.onUploadReady(ready)
  expect(importUpload).toHaveBeenCalledTimes(1)
  controller.begin(target)
  importUpload.mockRejectedValueOnce(new Error('Try again.'))
  await controller.onUploadReady({ ...ready, status: { ...ready.status, extracted_version: 3 } })
  expect(controller.getSnapshot().phase).toBe('attach-failed')
  await controller.retry()
  expect(importUpload).toHaveBeenCalledTimes(3)
  expect(importUpload.mock.calls[2]).toEqual(importUpload.mock.calls[1])
})

it('B3 row14 clears ready listeners when a disposed controller starts again', async () => {
  const upload = createDrawingUploadController({ services: uploadServices() })
  const oldListener = vi.fn()
  upload.subscribeReady(oldListener)
  upload.dispose()
  upload.start()
  const newListener = vi.fn()
  upload.subscribeReady(newListener)
  await upload.loadPolicy()
  await upload.upload({ name: 'site.dwg', size: 1 })
  expect(upload.getSnapshot().phase).toBe('ready')
  expect(newListener).toHaveBeenCalledTimes(1)
  expect(oldListener).not.toHaveBeenCalled()
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
