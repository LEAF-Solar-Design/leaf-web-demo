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

  it('updates a matching visible key in place, preserving id, length, and older history', () => {
    const bus = createNotificationBus(2)
    const olderId = bus.push({ text: 'older' })
    const id = bus.push({ text: 'Saving', key: 'save' })
    const before = bus.getSnapshot()
    const listener = vi.fn()
    bus.subscribe(listener)
    const action = { label: 'Undo', undo: true, onClick: vi.fn() }
    expect(bus.push({ text: 'Saved', kind: 'success', action, key: 'save' })).toBe(id)
    const after = bus.getSnapshot()
    expect(after).not.toBe(before)
    expect(after.ring).not.toBe(before.ring)
    expect(after.visibleId).toBe(id)
    expect(after.ring).toHaveLength(2)
    expect(after.ring.map((n) => n.id)).toEqual([id, olderId])
    expect(after.ring[1]).toBe(before.ring[1])
    expect(after.ring[0]).toMatchObject({ id, text: 'Saved', kind: 'success', key: 'save', action })
    expect(before.ring[0].text).toBe('Saving')
    expect(listener).toHaveBeenCalledTimes(1)
    // Updating does not consume the next newly minted id.
    expect(bus.push({ text: 'next' })).toBe(id + 1)
  })

  it('a keyed update replaces the action as well as the message', () => {
    const bus = createNotificationBus()
    const id = bus.push({ text: 'first', key: 'job', action: { onClick: vi.fn() } })
    expect(bus.push({ text: 'second', key: 'job', action: { label: 'invalid' } })).toBe(id)
    expect(bus.getSnapshot().ring).toHaveLength(1)
    expect(bus.getSnapshot().ring[0].action).toBeNull()
  })

  it('only coalesces the visible key, never an older or dismissed notice', () => {
    const bus = createNotificationBus()
    const first = bus.push({ text: 'first', key: 'save' })
    bus.push({ text: 'other', key: 'other' })
    const next = bus.push({ text: 'again', key: 'save' })
    expect(next).not.toBe(first)
    expect(bus.getSnapshot().ring).toHaveLength(3)
    bus.dismissVisible(next)
    const afterDismiss = bus.push({ text: 'new save', key: 'save' })
    expect(afterDismiss).not.toBe(next)
    expect(bus.getSnapshot().ring).toHaveLength(4)
  })

  it('unkeyed pushes stay distinct, while an explicit empty key can update', () => {
    const bus = createNotificationBus()
    const first = bus.push({ text: 'one' })
    expect(bus.push({ text: 'two' })).not.toBe(first)
    expect(bus.getSnapshot().ring).toHaveLength(2)
    const keyed = bus.push({ text: 'three', key: '' })
    expect(bus.push({ text: 'updated', key: '' })).toBe(keyed)
    expect(bus.getSnapshot().ring).toHaveLength(3)
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
