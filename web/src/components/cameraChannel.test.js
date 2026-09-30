// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { createCameraChannel } from './cameraChannel.js'

function setup() {
  let id = 0
  const queued = new Map()
  const channel = createCameraChannel({ schedule: (fn) => { queued.set(++id, fn); return id }, cancel: (key) => queued.delete(key) })
  return { channel, queued, flush: () => { const jobs = [...queued.values()]; queued.clear(); jobs.forEach((fn) => fn()) } }
}

it('delivers initially synchronously and coalesces later updates per frame', () => {
  const { channel, queued, flush } = setup(), listener = vi.fn()
  channel.subscribe(listener)
  expect(listener).toHaveBeenLastCalledWith({ pose: null, viewport: null })
  channel.publish({ pose: 1 }); channel.publish({ pose: 2 })
  expect(listener).toHaveBeenCalledTimes(1)
  expect(queued.size).toBe(1)
  flush()
  expect(listener).toHaveBeenLastCalledWith({ pose: 2 })
  const late = vi.fn(); channel.subscribe(late)
  expect(late).toHaveBeenLastCalledWith({ pose: 2 })
})

it('isolates throwing listeners and preserves registrations when an old scene is cancelled', () => {
  const { channel, queued, flush } = setup(), listener = vi.fn()
  channel.subscribe(() => { throw Error('consumer') })
  const off = channel.subscribe(listener)
  channel.publish({ pose: 'old' })
  const stale = [...queued.values()][0]
  channel.cancelPending()
  stale()
  expect(listener).toHaveBeenCalledTimes(1)
  channel.publish({ pose: 'new' }); flush()
  expect(listener).toHaveBeenLastCalledWith({ pose: 'new' })
  off(); channel.publish({ pose: 'after unsubscribe' }); flush()
  expect(listener).toHaveBeenCalledTimes(2)
})

it('dispose cancels queued delivery and removes all registrations', () => {
  const { channel, queued, flush } = setup(), listener = vi.fn()
  channel.subscribe(listener); channel.publish({ pose: 1 })
  channel.dispose(); channel.publish({ pose: 2 }); channel.subscribe(listener); flush()
  expect(queued.size).toBe(0)
  expect(listener).toHaveBeenCalledTimes(1)
})
