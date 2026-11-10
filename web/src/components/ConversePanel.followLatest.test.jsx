import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
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

const setup = async () => {
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
  log.scrollTop = 0
  fireEvent.scroll(log)
  expect(jumpChip()).not.toBeNull()
  return { view, log, resize }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('following the latest conversation message', () => {
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
