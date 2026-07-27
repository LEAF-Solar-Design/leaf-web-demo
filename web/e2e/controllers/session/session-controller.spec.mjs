import { expect, test } from '@playwright/test'
import { createSessionController } from '../../../src/controllers/session/createSessionController.js'

test('session controller latches expiry, clears the token, and needs explicit activation', async () => {
  const values = new Map([['leaf.jwt', 'token']])
  const storage = {
    getItem: (key) => values.get(key) || null,
    removeItem: (key) => values.delete(key),
  }
  let unauthorized
  const controller = createSessionController({
    storage,
    subscribeUnauthorized: (listener) => { unauthorized = listener; return () => { unauthorized = null } },
  })
  controller.start()
  controller.actions.activate({ tenant: 'tenant-a' })
  expect(controller.getSnapshot()).toMatchObject({ status: 'active', generation: 1 })

  unauthorized('/api/jobs')
  unauthorized('/api/catalog')
  expect(storage.getItem('leaf.jwt')).toBeNull()
  expect(controller.getSnapshot()).toMatchObject({
    status: 'required', reason: 'expired', generation: 1,
    sources: ['/api/jobs', '/api/catalog'],
  })

  controller.actions.checking()
  expect(controller.getSnapshot().status).toBe('required')
  controller.actions.activate({ tenant: 'tenant-a' })
  expect(controller.getSnapshot()).toMatchObject({ status: 'active', generation: 2, sources: [] })
  controller.destroy()
})

test('sign out paints required state before ending the external session', async () => {
  let release
  const ended = new Promise((resolve) => { release = resolve })
  const controller = createSessionController({
    storage: { removeItem() {} },
    endSession: () => ended,
  })
  const pending = controller.actions.signOut()
  expect(controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'signed_out' })
  release()
  await pending
})
