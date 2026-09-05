import { describe, expect, it, vi } from 'vitest'

import { createNotificationBus, RING_CAPACITY } from './notifications.js'

describe('notification bus (slice 13a)', () => {
  it('bounds the ring at its capacity, dropping the oldest notice first', () => {
    const bus = createNotificationBus(3)
    bus.push({ text: 'one' })
    bus.push({ text: 'two' })
    bus.push({ text: 'three' })
    bus.push({ text: 'four' })
    const { ring } = bus.getSnapshot()
    expect(ring).toHaveLength(3)
    expect(ring.map((n) => n.text)).toEqual(['four', 'three', 'two'])
  })

  it('never grows past capacity across many pushes', () => {
    const bus = createNotificationBus(5)
    for (let i = 0; i < 200; i += 1) bus.push({ text: `n${i}` })
    expect(bus.getSnapshot().ring.length).toBeLessThanOrEqual(5)
  })

  it('defaults to the module-exported RING_CAPACITY', () => {
    const bus = createNotificationBus()
    for (let i = 0; i < RING_CAPACITY + 10; i += 1) bus.push({ text: `n${i}` })
    expect(bus.getSnapshot().ring.length).toBe(RING_CAPACITY)
  })

  it('the newest notice always replaces the visible toast', () => {
    const bus = createNotificationBus(10)
    const id1 = bus.push({ text: 'first' })
    expect(bus.getSnapshot().visibleId).toBe(id1)
    const id2 = bus.push({ text: 'second' })
    expect(bus.getSnapshot().visibleId).toBe(id2)
    expect(bus.getSnapshot().visibleId).not.toBe(id1)
  })

  it('keeps EVERY pushed notice (kind, time, action) in the ring for the inbox, even once it is no longer visible', () => {
    const onClick = vi.fn()
    const bus = createNotificationBus(10)
    const id1 = bus.push({ text: 'job done', kind: 'success', action: { label: 'View', onClick } })
    bus.push({ text: 'job two' })
    const { ring, visibleId } = bus.getSnapshot()
    expect(visibleId).not.toBe(id1) // no longer visible...
    const kept = ring.find((n) => n.id === id1)
    expect(kept).toBeTruthy() // ...but still kept
    expect(kept.kind).toBe('success')
    expect(kept.text).toBe('job done')
    expect(typeof kept.time).toBe('number')
    expect(kept.action.label).toBe('View')
    kept.action.onClick()
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('dismissVisible only clears a MATCHING visible id (stale id is a no-op)', () => {
    const bus = createNotificationBus(10)
    const id1 = bus.push({ text: 'first' })
    const id2 = bus.push({ text: 'second' })
    bus.dismissVisible(id1) // stale: id2 is visible now
    expect(bus.getSnapshot().visibleId).toBe(id2)
    bus.dismissVisible(id2)
    expect(bus.getSnapshot().visibleId).toBeNull()
    // the ring still holds both notices — dismissVisible never drops history
    expect(bus.getSnapshot().ring).toHaveLength(2)
  })

  it('clearVisible unconditionally clears whichever notice is showing', () => {
    const bus = createNotificationBus(10)
    bus.push({ text: 'first' })
    bus.clearVisible()
    expect(bus.getSnapshot().visibleId).toBeNull()
  })

  it('every subscribe has a matching unsubscribe: listener count returns to zero', () => {
    const bus = createNotificationBus(10)
    const unsubA = bus.subscribe(() => {})
    const unsubB = bus.subscribe(() => {})
    expect(bus.listenerCount()).toBe(2)
    unsubA()
    expect(bus.listenerCount()).toBe(1)
    unsubB()
    expect(bus.listenerCount()).toBe(0)
  })

  it('notifies every live subscriber on push and on dismiss, and never a dropped one', () => {
    const bus = createNotificationBus(10)
    const seenA = []
    const seenB = []
    const unsubA = bus.subscribe(() => seenA.push(1))
    bus.subscribe(() => seenB.push(1))
    bus.push({ text: 'x' })
    unsubA()
    bus.push({ text: 'y' })
    expect(seenA).toHaveLength(1) // unsubscribed before the second push
    expect(seenB).toHaveLength(2)
  })

  it('rejects a non-positive-integer capacity rather than silently building an unbounded ring', () => {
    expect(() => createNotificationBus(0)).toThrow()
    expect(() => createNotificationBus(-1)).toThrow()
    expect(() => createNotificationBus(1.5)).toThrow()
  })
})
