import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../engineChanges.js', () => ({
  listEngineChanges: vi.fn(() => Promise.resolve({ kind: 'forbidden' })),
}))
// Model a log behind a conditional without changing the panel's public props.
const logRendering = vi.hoisted(() => ({ visible: true }))
vi.mock('./LiveRegion.jsx', async (importOriginal) => {
  const { default: LiveRegion } = await importOriginal()
  const { forwardRef } = await import('react')
  return {
    default: forwardRef(function DeferredLiveRegion(props, ref) {
      if (props.role === 'log' && !logRendering.visible) return null
      return <LiveRegion {...props} ref={ref} />
    }),
  }
})
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'

const event = (type, turn_id, data = {}) => ({ type, turn_id, data })
const push = async (...events) => {
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}
const panel = (userTurns) => <ConversePanel sessionId="session" onDismiss={vi.fn()} userTurns={userTurns} />
const initialTurns = [{ turnId: 'original', text: 'First request' }]
const jumpChip = () => screen.queryByRole('button', { name: /jump to latest/i })
let observers

beforeEach(() => {
  logRendering.visible = true
  observers = []
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback) {
      this.callback = callback
      this.observe = vi.fn()
      this.disconnect = vi.fn()
      observers.push(this)
    }
    report() { this.callback([]) }
  })
})

const setup = async ({ scrollAway = true } = {}) => {
  const view = render(panel(initialTurns))
  await push(event('turn_started', 'original'))
  const log = screen.getByRole('log', { name: 'Assistant conversation' })
  let height = 394
  Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 2000 })
  Object.defineProperty(log, 'clientHeight', { configurable: true, get: () => height })
  const resize = (nextHeight) => {
    height = nextHeight
    act(() => observers[0].report())
  }
  resize(height)
  if (scrollAway) {
    fireEvent.wheel(log)
    log.scrollTop = 0
    fireEvent.scroll(log)
    expect(jumpChip()).not.toBeNull()
  }
  return { view, log, resize }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('following the latest conversation message', () => {
  it('re-pins a layout scroll without a reader gesture', async () => {
    const { log } = await setup({ scrollAway: false })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 43100 })
    log.scrollTop = 651
    fireEvent.scroll(log)
    expect(jumpChip()).toBeNull()
    expect(log.scrollTop).toBe(log.scrollHeight)
  })

  it('follows content appended after the last event', async () => {
    const { log } = await setup({ scrollAway: false })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 30000 })
    await act(async () => { log.appendChild(document.createElement('div')) })
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
  })

  it('keeps the reader’s position when content is appended after scrolling away', async () => {
    const { log } = await setup()
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 30000 })
    await act(async () => { log.appendChild(document.createElement('div')) })
    expect(log.scrollTop).toBe(0)
    expect(jumpChip()).not.toBeNull()
  })

  it('releases the pin after a scroll key inside the log', async () => {
    const { log } = await setup({ scrollAway: false })
    fireEvent.keyDown(log, { key: 'PageUp' })
    log.scrollTop = 0
    fireEvent.scroll(log)
    expect(log.scrollTop).toBe(0)
    expect(jumpChip()).not.toBeNull()
  })

  it('opens at the newest message with a current user turn and a long transcript', async () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(30000)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(394)
    const history = Array.from({ length: 100 }, (_, i) => ({
      turnId: `older-${i}`, text: `Older request ${i}`,
    }))
    converse.openStream.mockImplementationOnce((_sessionId, _afterSeq, handlers) => {
      history.forEach(({ turnId }) => {
        handlers.onEvent(event('turn_started', turnId))
        handlers.onEvent(event('text_delta', turnId, { text: 'An older reply.' }))
      })
      handlers.onEvent(event('turn_started', 'original'))
      return { close: vi.fn() }
    })
    await act(async () => { render(panel([...history, ...initialTurns])) })
    const log = screen.getByRole('log', { name: 'Assistant conversation' })
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
  })

  it('attaches the observer when the log appears after mount and when it is replaced', async () => {
    logRendering.visible = false
    const view = render(panel(initialTurns))
    await push(event('turn_started', 'original'))
    expect(screen.queryByRole('log')).toBeNull()
    expect(observers).toHaveLength(0)

    logRendering.visible = true
    view.rerender(panel(initialTurns))
    const log = screen.getByRole('log', { name: 'Assistant conversation' })
    expect(observers).toHaveLength(1)
    expect(observers[0].observe).toHaveBeenCalledWith(log)
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 30000 })
    let height = 0
    Object.defineProperty(log, 'clientHeight', { configurable: true, get: () => height })
    act(() => observers[0].report())
    height = 394
    act(() => observers[0].report())
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()

    logRendering.visible = false
    view.rerender(panel(initialTurns))
    expect(observers[0].disconnect).toHaveBeenCalledOnce()
    logRendering.visible = true
    view.rerender(panel(initialTurns))
    const replacement = screen.getByRole('log', { name: 'Assistant conversation' })
    expect(replacement).not.toBe(log)
    expect(observers).toHaveLength(2)
    expect(observers[1].observe).toHaveBeenCalledWith(replacement)
    view.unmount()
    expect(observers[1].disconnect).toHaveBeenCalledOnce()
  })

  it('jumps to latest for the reader’s own new turn and hides the chip', async () => {
    const { view, log } = await setup()
    await act(async () => {
      view.rerender(panel([...initialTurns, { turnId: 'next', text: 'Next request' }]))
      converse.openStream.mock.calls.at(-1)[2].onEvent(event('turn_started', 'next'))
    })
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
  })

  it('keeps the reader’s position for more events in the same turn', async () => {
    const { log } = await setup()
    await push(event('text_delta', 'original', { text: 'More of the reply.' }))
    expect(log.scrollTop).toBe(0)
    expect(jumpChip()).not.toBeNull()
  })

  it('keeps following a large history backfill after a jump', async () => {
    const { log } = await setup()
    fireEvent.click(jumpChip())
    expect(log.scrollTop).toBe(log.scrollHeight)
    fireEvent.scroll(log)
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 30000 })
    await push(...Array.from({ length: 100 }, (_, i) =>
      event('text_delta', `older-${i}`, { text: `Backfilled reply ${i}` })))
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
  })

  it('re-pins a hidden log when it becomes visible and disconnects on unmount', async () => {
    const { view, log, resize } = await setup()
    expect(observers[0].observe).toHaveBeenCalledWith(log)
    resize(0)
    expect(log.scrollTop).toBe(0)
    resize(394)
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
    view.unmount()
    expect(observers[0].disconnect).toHaveBeenCalledOnce()
  })

  it('preserves the scroll lock on visible resizes', async () => {
    const { log, resize } = await setup()
    resize(300)
    expect(log.scrollTop).toBe(0)
    expect(jumpChip()).not.toBeNull()
    fireEvent.click(jumpChip())
    log.scrollTop = 1500
    resize(394)
    expect(log.scrollTop).toBe(log.scrollHeight)
    expect(jumpChip()).toBeNull()
  })
})
