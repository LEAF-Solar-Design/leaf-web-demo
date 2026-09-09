// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import StatusModesBridge from './StatusModesBridge.jsx'

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
    expect(last(published)).toEqual({ live: true, ortho: false, osnap: true })
    expect(studio.createWorker).not.toHaveBeenCalled()
  }))

  it('answers requests with the current provider state', () => observe((published) => {
    const studio = mount()
    act(() => studio.context.setOrtho(true))
    published.mockClear()
    dispatch('cockpit:modes-request')
    expect(published).toHaveBeenCalledTimes(1)
    expect(last(published)).toEqual({ live: true, ortho: true, osnap: true })
  }))

  it('toggles ORTHO in the provider and publishes both changes', () => observe((published) => {
    const studio = mount()
    for (const ortho of [true, false]) {
      published.mockClear()
      dispatch('cockpit:mode-toggle', { id: 'ortho' })
      expect(studio.context.ortho).toBe(ortho)
      expect(published).toHaveBeenCalledTimes(1)
      expect(last(published)).toEqual({ live: true, ortho, osnap: true })
    }
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
    expect(last(published)).toEqual({ live: true, ortho: false, osnap: false })
    dispatch('cockpit:mode-toggle', { id: 'osnap' })
    expect(studio.context.osnap).toBe(true)
    expect(last(published)).toEqual({ live: true, ortho: false, osnap: true })
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
})
