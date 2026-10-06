// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import StatusModesBridge from './StatusModesBridge.jsx'
import { ALL_SNAP_MODES, DEFAULT_SNAP_MODES } from './snapModes.js'

afterEach(cleanup)

// The scripted transport and context probe follow engineSessionProvider.test.jsx.
class ScriptedWorker {
  constructor() {
    this.posted = []
    this.listeners = { message: [], error: [], messageerror: [] }
    this.terminated = false
  }
  addEventListener(type, cb) {
    if (this.listeners[type]) this.listeners[type].push(cb)
  }
  removeEventListener() {}
  postMessage(data) { this.posted.push(data) }
  terminate() { this.terminated = true }
  emit(data) {
    act(() => { this.listeners.message.forEach((cb) => cb({ data })) })
  }
  die() {
    act(() => { this.listeners.error.forEach((cb) => cb({ type: 'error' })) })
  }
}

function mount() {
  const workers = []
  const createWorker = vi.fn(() => {
    const worker = new ScriptedWorker()
    workers.push(worker)
    return worker
  })
  const handle = { workers, createWorker }
  function Probe() {
    handle.context = useEngineSessionContext()
    return null
  }
  const utils = render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <StatusModesBridge />
    </EngineSessionProvider>,
  )
  handle.unmount = utils.unmount
  return handle
}

const dispatch = (name, detail) => act(() => { window.dispatchEvent(new CustomEvent(name, { detail })) })
// B1b: a live publication carries the snap mode mask and the limitation flag
// beside the two masters.
const LIVE = Object.freeze({ live: true, ortho: false, osnap: true, snapModes: DEFAULT_SNAP_MODES, snapLimited: false })
const live = (over = {}) => ({ ...LIVE, ...over })

describe('StatusModesBridge', () => {
  function observe(run) {
    const published = vi.fn()
    window.addEventListener('cockpit:modes', published)
    try { run(published) } finally {
      cleanup()
      window.removeEventListener('cockpit:modes', published)
    }
  }
  const last = (published) => published.mock.calls.at(-1)[0].detail

  it('publishes the default provider modes on mount', () => observe((published) => {
    const studio = mount()
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual(live())
    expect(studio.createWorker).not.toHaveBeenCalled()
  }))

  it('answers requests with the current provider state', () => observe((published) => {
    const studio = mount()
    act(() => studio.context.setOrtho(true))
    published.mockClear()
    dispatch('cockpit:modes-request')
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual(live({ ortho: true }))
  }))

  it('toggles ORTHO in the provider and publishes both changes', () => observe((published) => {
    const studio = mount()
    for (const ortho of [true, false]) {
      published.mockClear()
      dispatch('cockpit:mode-toggle', { id: 'ortho' })
      expect(studio.context.ortho).toBe(ortho)
      expect(published).toHaveBeenCalledTimes(1)
      expect(last(published)).toEqual(live({ ortho }))
    }
  }))

  it('m: applies two and three synchronous ORTHO toggles', () => observe((published) => {
    const studio = mount()
    for (const [count, ortho] of [[2, false], [3, true]]) {
      act(() => {
        for (let i = 0; i < count; i++) {
          window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'ortho' } }))
        }
      })
      expect(studio.context.ortho).toBe(ortho)
      expect(last(published)).toEqual(live({ ortho }))
    }
  }))

  it('n: applies two and three synchronous OSNAP toggles', () => observe((published) => {
    const studio = mount()
    for (const [count, osnap] of [[2, true], [3, false]]) {
      act(() => {
        for (let i = 0; i < count; i++) {
          window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'osnap' } }))
        }
      })
      expect(studio.context.osnap).toBe(osnap)
      expect(last(published)).toEqual(live({ osnap }))
    }
  }))

  it('o: applies synchronous ORTHO and OSNAP toggles once each', () => observe((published) => {
    const studio = mount()
    act(() => {
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'ortho' } }))
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'osnap' } }))
    })
    expect(studio.context.ortho).toBe(true)
    expect(studio.context.osnap).toBe(false)
    expect(last(published)).toEqual(live({ ortho: true, osnap: false }))
  }))

  it('ignores unsupported ids and non-object details', () => observe((published) => {
    const studio = mount()
    published.mockClear()
    for (const detail of [{ id: 'grid' }, { id: 42 }, null, 'ortho']) {
      expect(() => dispatch('cockpit:mode-toggle', detail)).not.toThrow()
      expect(studio.context.ortho).toBe(false)
      expect(studio.context.osnap).toBe(true)
      expect(published).not.toHaveBeenCalled()
    }
  }))

  it('follows OSNAP changes made directly through the provider', () => observe((published) => {
    const studio = mount()
    published.mockClear()
    act(() => studio.context.setOsnap(false))
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual(live({ osnap: false }))
    dispatch('cockpit:mode-toggle', { id: 'osnap' })
    expect(studio.context.osnap).toBe(true)
    expect(last(published)).toEqual(live())
  }))

  it('publishes offline and removes both listeners on unmount', () => observe((published) => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const studio = mount()
      const listeners = add.mock.calls.filter(([type]) => ['cockpit:modes-request', 'cockpit:mode-toggle'].includes(type))
      expect(listeners).toHaveLength(2)
      published.mockClear()
      studio.unmount()
      expect(published).toHaveBeenCalledTimes(1)
      expect(last(published)).toEqual({ live: false })
      for (const [type, listener] of listeners) expect(remove).toHaveBeenCalledWith(type, listener)
      published.mockClear()
      expect(() => dispatch('cockpit:mode-toggle', { id: 'ortho' })).not.toThrow()
      dispatch('cockpit:modes-request')
      expect(published).not.toHaveBeenCalled()
      expect(studio.context.ortho).toBe(false)
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  }))

  it('B1B-B01 complete publication and request', () => observe((published) => {
    const studio = mount()
    expect(last(published)).toEqual({ live: true, ortho: false, osnap: true, snapModes: 23, snapLimited: false })
    expect(Object.keys(last(published)).sort()).toEqual(['live', 'ortho', 'osnap', 'snapLimited', 'snapModes'])
    published.mockClear()
    act(() => { studio.context.setSnapMode('intersection', true); studio.context.setSnapLimited(true) })
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual(live({ snapModes: 23 | 32, snapLimited: true }))
    published.mockClear()
    dispatch('cockpit:modes-request')
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual(live({ snapModes: 55, snapLimited: true }))
  }))

  it('B1B-B02 absolute checkbox requests', () => observe((published) => {
    const studio = mount()
    published.mockClear()
    dispatch('cockpit:osnap-mode-set', { kind: 'intersection', enabled: true })
    expect(studio.context.snapModes).toBe(55)
    expect(last(published)).toEqual(live({ snapModes: 55 }))
    // Absolute, never a toggle: the same request again changes nothing.
    published.mockClear()
    dispatch('cockpit:osnap-mode-set', { kind: 'intersection', enabled: true })
    expect(studio.context.snapModes).toBe(55)
    expect(published).not.toHaveBeenCalled()
    dispatch('cockpit:osnap-mode-set', { kind: 'endpoint', enabled: false })
    expect(studio.context.snapModes).toBe(54)
    // A mode request never touches either master.
    expect(studio.context.osnap).toBe(true)
    expect(studio.context.ortho).toBe(false)
    act(() => studio.context.setOsnap(false))
    dispatch('cockpit:osnap-mode-set', { kind: 'nearest', enabled: true })
    expect(studio.context.snapModes).toBe(54 | 512)
    expect(studio.context.osnap).toBe(false)
    for (const { kind } of [{ kind: 'endpoint' }, { kind: 'midpoint' }, { kind: 'centre' }, { kind: 'quadrant' }, { kind: 'intersection' }, { kind: 'insertion' }, { kind: 'perpendicular' }, { kind: 'tangent' }, { kind: 'nearest' }]) {
      dispatch('cockpit:osnap-mode-set', { kind, enabled: true })
    }
    expect(studio.context.snapModes).toBe(ALL_SNAP_MODES)
    expect(last(published)).toEqual(live({ osnap: false, snapModes: ALL_SNAP_MODES }))
  }))

  it('B1B-B03 malformed checkbox requests ignored', () => observe((published) => {
    const studio = mount()
    published.mockClear()
    for (const detail of [
      null, 'intersection', 42, {}, { kind: 'intersection' }, { kind: 'intersection', enabled: 'true' },
      { kind: 'intersection', enabled: 1 }, { kind: 'intersection', enabled: undefined }, { kind: 'bogus', enabled: true },
      { kind: 32, enabled: true }, { kind: 'constructor', enabled: true }, { kind: 'Endpoint', enabled: false }, { id: 'osnap' },
    ]) {
      expect(() => dispatch('cockpit:osnap-mode-set', detail)).not.toThrow()
      expect(studio.context.snapModes).toBe(DEFAULT_SNAP_MODES)
      expect(studio.context.osnap).toBe(true)
      expect(published).not.toHaveBeenCalled()
    }
  }))

  it('B1B-B04 offline and all listener cleanup', () => observe((published) => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const studio = mount()
      const types = ['cockpit:modes-request', 'cockpit:mode-toggle', 'cockpit:osnap-mode-set']
      const listeners = add.mock.calls.filter(([type]) => types.includes(type))
      expect(listeners.map(([type]) => type).sort()).toEqual([...types].sort())
      published.mockClear()
      studio.unmount()
      expect(published).toHaveBeenCalledTimes(1)
      expect(last(published)).toEqual({ live: false })
      for (const [type, listener] of listeners) expect(remove).toHaveBeenCalledWith(type, listener)
      published.mockClear()
      dispatch('cockpit:osnap-mode-set', { kind: 'nearest', enabled: true })
      dispatch('cockpit:modes-request')
      expect(published).not.toHaveBeenCalled()
      expect(studio.context.snapModes).toBe(DEFAULT_SNAP_MODES)
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  }))

  it('B1B-B05 synchronous master toggles preserved', () => observe((published) => {
    const studio = mount()
    act(() => studio.context.setSnapMode('tangent', true))
    act(() => {
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'osnap' } }))
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'osnap' } }))
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'osnap' } }))
      window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: 'ortho' } }))
    })
    expect(studio.context.osnap).toBe(false)
    expect(studio.context.ortho).toBe(true)
    // The master switch keeps the selected modes.
    expect(studio.context.snapModes).toBe(23 | 256)
    expect(last(published)).toEqual(live({ ortho: true, osnap: false, snapModes: 23 | 256 }))
    dispatch('cockpit:mode-toggle', { id: 'osnap' })
    expect(last(published)).toEqual(live({ ortho: true, snapModes: 23 | 256 }))
  }))
})
