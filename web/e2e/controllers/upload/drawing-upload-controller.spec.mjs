import { expect, test } from '@playwright/test'
import { createDrawingUploadController } from '../../../src/controllers/upload/createDrawingUploadController.js'

const file = { name: 'roof.dxf', size: 128 }

test('valid upload waits for extraction, loads intake, and publishes ready', async () => {
  const calls = []
  let ready = null
  const controller = createDrawingUploadController({
    pollMs: 0,
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
      upload: async () => { calls.push('upload'); return { drawing_id: 'drawing-1', status: 'extracting' } },
      wait: async () => {},
      status: async () => { calls.push('status'); return { status: 'ready' } },
      intake: async () => { calls.push('intake'); return { intake: { dwg: 'roof.dxf' }, version: 1 } },
    },
    onReady: (result) => { ready = result },
  })
  await controller.loadPolicy()
  const result = await controller.upload(file)
  expect(calls).toEqual(['upload', 'status', 'intake'])
  expect(result.view.intake.dwg).toBe('roof.dxf')
  expect(ready.receipt.drawing_id).toBe('drawing-1')
  expect(controller.getSnapshot()).toMatchObject({ phase: 'ready', busy: false, error: null })
})

test('invalid type and oversize files never call upload', async () => {
  let calls = 0
  const controller = createDrawingUploadController({
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 100 }),
      upload: async () => { calls += 1 }, wait: async () => {}, status: async () => ({}), intake: async () => ({}),
    },
  })
  await controller.loadPolicy()
  await controller.upload({ name: 'roof.exe', size: 10 })
  expect(controller.getSnapshot().error).toContain('.dxf')
  await controller.upload({ name: 'roof.dxf', size: 101 })
  expect(controller.getSnapshot().error).toContain('upload limit')
  expect(calls).toBe(0)
})

test('exhausted polling reports still-running, not a failure the server never declared', async () => {
  const controller = createDrawingUploadController({
    pollMs: 0,
    maxPolls: 2,
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
      upload: async () => ({ drawing_id: 'drawing-slow', status: 'extracting' }),
      wait: async () => {},
      status: async () => ({ status: 'extracting' }),
      intake: async () => ({}),
    },
  })
  await controller.loadPolicy()
  const result = await controller.upload(file)
  expect(result).toBeNull()
  expect(controller.getSnapshot().phase).toBe('failed')
  expect(controller.getSnapshot().error).toContain('still running')
  expect(controller.getSnapshot().error).not.toContain('did not finish')
})

test('a server-declared failure surfaces the server message verbatim', async () => {
  const controller = createDrawingUploadController({
    pollMs: 0,
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
      upload: async () => ({ drawing_id: 'drawing-big', status: 'extracting' }),
      wait: async () => {},
      status: async () => ({ status: 'failed', error: { message: 'WorkItem hit the processing time limit' } }),
      intake: async () => ({}),
    },
  })
  await controller.loadPolicy()
  await controller.upload(file)
  expect(controller.getSnapshot().error).toContain('processing time limit')
})

test('a failure arriving on the final allowed poll surfaces the server message, not still-running', async () => {
  const controller = createDrawingUploadController({
    pollMs: 0,
    maxPolls: 1,
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
      upload: async () => ({ drawing_id: 'drawing-last-poll', status: 'extracting' }),
      wait: async () => {},
      status: async () => ({ status: 'failed', error: { message: 'SERVER VERBATIM: processing time limit' } }),
      intake: async () => ({}),
    },
  })
  await controller.loadPolicy()
  await controller.upload(file)
  expect(controller.getSnapshot().error).toContain('SERVER VERBATIM')
  expect(controller.getSnapshot().error).not.toContain('still running')
})

test('cancel prevents a late upload receipt from loading or seating intake', async () => {
  let release
  let intakeCalls = 0
  const gate = new Promise((resolve) => { release = resolve })
  const controller = createDrawingUploadController({
    services: {
      policy: async () => ({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
      upload: async () => gate,
      wait: async () => {}, status: async () => ({ status: 'ready' }),
      intake: async () => { intakeCalls += 1; return {} },
    },
  })
  await controller.loadPolicy()
  const pending = controller.upload(file)
  controller.cancel()
  release({ drawing_id: 'late', status: 'ready' })
  await pending
  expect(intakeCalls).toBe(0)
  expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', busy: false })
})
