import { expect, test } from '@playwright/test'
import { createSessionController } from '../../../src/controllers/session/createSessionController.js'

test('session controller latches expiry, clears the token, and needs explicit activation until a re-verified proof arrives through recovery', async () => {
  const values = new Map([['leaf.jwt', 'token']])
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
  let unauthorized
  let tokenStored
  const controller = createSessionController({
    storage,
    subscribeUnauthorized: (listener) => { unauthorized = listener; return () => { unauthorized = null } },
    subscribeTokenStored: (listener) => { tokenStored = listener; return () => { tokenStored = null } },
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

  // b5b4afeb (#880, adversarially verified): a latched `required` gate
  // outranks a stale session proof. A stray activate() while still latched
  // must not clear it -- the object is untouched (same generation, same
  // status), because the proof it carries cannot be trusted against a
  // refusal that landed after it.
  controller.actions.activate({ tenant: 'tenant-a' })
  expect(controller.getSnapshot()).toMatchObject({ status: 'required', generation: 1 })

  // The sanctioned recovery path (createSessionController.test.js's own
  // regression cover): a fresh token stored through the real channel
  // (auth.js storeToken -> subscribeTokenStored) is the only thing that
  // re-opens `checking`. Only from there does a re-verified activate() land.
  storage.setItem('leaf.jwt', 'fresh-token')
  tokenStored('fresh-token')
  expect(controller.getSnapshot().status).toBe('checking')
  controller.actions.activate({ tenant: 'tenant-a' })
  expect(controller.getSnapshot()).toMatchObject({ status: 'active', generation: 2, sources: [] })
  controller.destroy()
})

test('sign out paints required state before ending the external session', async () => {
  let release
  const removed = []
  const ended = new Promise((resolve) => { release = resolve })
  const controller = createSessionController({
    storage: { removeItem(key) { removed.push(key) } },
    endSession: () => ended,
  })
  const pending = controller.actions.signOut()
  expect(controller.getSnapshot()).toMatchObject({ status: 'required', reason: 'signed_out' })
  expect(removed).toContain('leaf.inflightAuthor.v1')
  release()
  await pending
})
