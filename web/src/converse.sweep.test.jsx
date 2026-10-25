import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('./telemetry.js', () => ({ track: vi.fn(), trackErrorShown: vi.fn(), trackStreamDown: vi.fn() }))
vi.mock('./api.js', () => ({
  config: { apiBase: '', tenant: 'test' }, authHeaders: () => ({}), noteUnauthorized: (res) => res,
}))
vi.mock('./engineChanges.js', () => ({ listEngineChanges: vi.fn(async () => ({ kind: 'forbidden' })) }))
vi.mock('./converse.js', async (original) => ({
  ...await original(),
  openStream: vi.fn(() => ({ close: vi.fn(), reconcile: vi.fn(async () => {}) })),
  postMessage: vi.fn(async () => ({ turn_id: 'sent', status: 'started' })),
  cancelTurn: vi.fn(async () => ({ status: 'cancelled' })),
  listPendingApprovals: vi.fn(async () => []),
}))

import * as converse from './converse.js'
import { track } from './telemetry.js'
import { agentBannerFor } from './lib/agentBanner.js'
import ConversePanel from './components/ConversePanel.jsx'

const handlers = () => converse.openStream.mock.calls.at(-1)[2]
const event = (type, turn_id, data = {}, seq = 1) => ({ type, turn_id, data, seq })
const push = async (...events) => act(async () => { events.forEach(handlers().onEvent) })
const setup = async () => {
  render(<ConversePanel sessionId="session" onDismiss={vi.fn()} />)
  await act(async () => {})
  return screen.getByRole('textbox', { name: 'Reply to the assistant' })
}
const paste = (input, size = 10) => {
  const file = new File([new Uint8Array(size)], 'roof.png', { type: 'image/png' })
  fireEvent.paste(input, { clipboardData: { items: [{ kind: 'file', type: file.type, getAsFile: () => file }] } })
}
const imageUrls = () => {
  vi.stubGlobal('URL', Object.assign(class extends URL {}, {
    createObjectURL: vi.fn(() => 'blob:roof'), revokeObjectURL: vi.fn(),
  }))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('conversation issue sweep', () => {
  it('requires a top-level code for busy, quota, and rate-limit classifications', () => {
    for (const [errorCode, kind] of [
      ['TURN_IN_PROGRESS', 'busy'],
      ['LLM_QUOTA_EXHAUSTED', 'quota'],
      ['LLM_RATE_LIMITED', 'rate_limited'],
    ]) {
      expect(converse.classifyAgentError({ status: 409, errorCode })).toBe(kind)
      expect(converse.classifyAgentError({ status: 409, body: { error: { error_code: errorCode } } })).toBe('unreachable')
    }
  })

  it('retains body-code fallback for access failures only', () => {
    for (const [error_code, kind] of [
      ['SESSION_EXPIRED', 'authentication'],
      ['GRANT_REQUIRED', 'grant'],
      ['ENTITLEMENT_REQUIRED', 'entitlement'],
      ['WORKSPACE_BINDING_REQUIRED', 'workspace'],
    ]) {
      expect(converse.classifyAgentError({ body: { error: { error_code } } })).toBe(kind)
    }
    expect(converse.classifyAgentError({ errorCode: 'TURN_IN_PROGRESS_X', body: { error: { error_code: 'TURN_IN_PROGRESS' } } })).toBe('unreachable')
  })

  it('S1-35 treats an expired login separately from a Claude grant', () => {
    expect(converse.classifyAgentError({ status: 401 })).toBe('authentication')
    expect(agentBannerFor({ status: 401 }).message).toMatch(/Sign in again/)
    expect(converse.classifyAgentError({ status: 401, body: { error: { error_code: 'grant_required' } } })).toBe('grant')
  })

  it('S1-35 does not describe missing workspace access as a plan restriction', () => {
    const error = { status: 403, body: { detail: 'verified subject has no active platform identity binding' } }
    expect(converse.classifyAgentError(error)).toBe('workspace')
    expect(agentBannerFor(error).message).toMatch(/workspace administrator/)
    expect(converse.classifyAgentError({ status: 403, errorCode: 'entitlement_required' })).toBe('entitlement')
  })

  it('S1-39 expands only the selected call across matching turns', async () => {
    await setup()
    await push(
      event('tool_call', 'first', { tool: 'count_panels', args: { layer: 'north' }, call_id: 'call' }, 1),
      event('tool_call', 'second', { tool: 'count_panels', args: { layer: 'south' }, call_id: 'call' }, 2),
    )
    const buttons = screen.getAllByRole('button', { name: /args/ })
    fireEvent.click(buttons[0])
    expect(buttons[0]).toHaveAttribute('aria-expanded', 'true')
    expect(buttons[1]).toHaveAttribute('aria-expanded', 'false')
  })

  it('S1-40 keeps accepted images and remove controls visible after a rejected paste', async () => {
    imageUrls()
    const input = await setup()
    paste(input)
    paste(input, 1024 * 1024 + 1)
    expect(screen.getByRole('alert')).toHaveTextContent('Each image must be 1MB or smaller.')
    expect(screen.getByAltText('Pending image attachment')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Remove image attachment' }))
    expect(screen.queryByAltText('Pending image attachment')).toBeNull()
  })

  it('S1-41 retains composer focus and prevents duplicate sends during a turn', async () => {
    const input = await setup()
    input.focus()
    fireEvent.change(input, { target: { value: 'Count panels' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(input).not.toBeDisabled()
    expect(input).toHaveFocus()
    expect(input.readOnly).toBe(true)
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(converse.postMessage).toHaveBeenCalledTimes(1)
    await push(event('turn_complete', 'sent', { stop_reason: 'end_turn' }))
    expect(input.readOnly).toBe(false)
    expect(input).toHaveFocus()
  })

  it('S1-41 returns focus from Send and Stop to the composer', async () => {
    const input = await setup()
    fireEvent.change(input, { target: { value: 'Count panels' } })
    const sendButton = screen.getByRole('button', { name: 'Send' })
    sendButton.focus()
    await act(async () => { fireEvent.click(sendButton) })
    expect(input).toHaveFocus()
    expect(input.readOnly).toBe(true)

    await push(event('turn_started', 'sent'))
    screen.getByRole('button', { name: 'Stop' }).focus()
    await push(event('turn_complete', 'sent', { stop_reason: 'end_turn' }, 2))

    expect(input).toHaveFocus()
    expect(input.readOnly).toBe(false)
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })

  it('S1-41 restores composer focus from the body when the turn ends', async () => {
    const input = await setup()
    await push(event('turn_started', 'running'))
    expect(document.activeElement).toBe(document.body)

    await push(event('turn_complete', 'running', { stop_reason: 'end_turn' }, 2))

    expect(input).toHaveFocus()
    expect(input.readOnly).toBe(false)
  })

  it('S1-42 displays a retry state when the stream drops', async () => {
    await setup()
    act(() => handlers().onStreamDown())
    expect(screen.getByRole('status')).toHaveTextContent('Retrying')
    fireEvent.click(screen.getByRole('button', { name: 'Retry connection' }))
    expect(converse.openStream.mock.results.at(-1).value.reconcile).toHaveBeenCalledTimes(1)
  })

  it('S1-42 unlocks an acknowledged stop without waiting for a stream event', async () => {
    const input = await setup()
    await push(event('turn_started', 'running'))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop' })) })
    expect(converse.cancelTurn).toHaveBeenCalledWith('session', 'running')
    expect(converse.openStream.mock.results.at(-1).value.reconcile).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(input).not.toBeDisabled()
    expect(input.readOnly).toBe(false)
  })

  it('S1-42 shows an acknowledged stop for a started turn when completion is lost', async () => {
    const input = await setup()
    fireEvent.change(input, { target: { value: 'Count panels' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await push(event('turn_started', 'sent'))
    expect(screen.getByTestId('converse-reasoning')).toHaveTextContent('thinking…')

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop' })) })

    expect(converse.cancelTurn).toHaveBeenCalledWith('session', 'sent')
    expect(converse.openStream.mock.results.at(-1).value.reconcile).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Reply stopped.')).toBeTruthy()
    expect(screen.queryByTestId('converse-reasoning')).toBeNull()
    expect(screen.queryByText('thinking…')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(input.readOnly).toBe(false)

    await push(event('turn_complete', 'sent', { stop_reason: 'interrupted' }, 2))
    expect(screen.queryByText('Reply stopped.')).toBeNull()
  })

  it('S1-42 unlocks an already ended turn with the stream down and no completion event', async () => {
    converse.cancelTurn.mockResolvedValueOnce({ status: 'already_ended' })
    const input = await setup()
    fireEvent.change(input, { target: { value: 'Count panels' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await push(event('turn_started', 'sent'))
    act(() => handlers().onStreamDown())
    expect(input.readOnly).toBe(true)

    screen.getByRole('button', { name: 'Stop' }).focus()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop' })) })

    expect(converse.cancelTurn).toHaveBeenCalledWith('session', 'sent')
    expect(converse.openStream.mock.results.at(-1).value.reconcile).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Reply stopped.')).toBeNull()
    expect(track).not.toHaveBeenCalledWith('conversation.truncated', expect.anything())
    expect(input.readOnly).toBe(false)
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(screen.queryByTestId('converse-reasoning')).toBeNull()
    expect(input).toHaveFocus()

    await push(event('turn_complete', 'sent', { stop_reason: 'end_turn' }, 2))
    expect(input).not.toBeDisabled()
    expect(input.readOnly).toBe(false)
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })

  it('S1-42 stops a pending turn without a live indicator when no stream event arrives', async () => {
    const input = await setup()
    fireEvent.change(input, { target: { value: 'Count panels' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    const turn = screen.getByText('Count panels').closest('.converse-turn')
    expect(turn.querySelector('.dot.live.pulse')).not.toBeNull()
    expect(input.readOnly).toBe(true)

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop' })) })

    expect(converse.cancelTurn).toHaveBeenCalledWith('session', 'sent')
    expect(converse.openStream.mock.results.at(-1).value.reconcile).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Reply stopped.')).toBeTruthy()
    expect(turn.querySelector('.dot.live')).toBeNull()
    expect(turn.querySelector('.pulse')).toBeNull()
    expect(screen.queryByText('thinking…')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(input).not.toBeDisabled()
    expect(input.readOnly).toBe(false)
  })

  it('S1-42 reports polling authentication failures instead of silently closing', async () => {
    const actual = await vi.importActual('./converse.js')
    vi.stubGlobal('EventSource', undefined)
    vi.stubGlobal('fetch', vi.fn(async () => ({ status: 401, ok: false })))
    const onConnectionState = vi.fn()
    const stream = actual.openStream('session', 0, { onConnectionState })
    try {
      await stream.reconcile()
      expect(onConnectionState).toHaveBeenCalledWith('authentication')
    } finally { stream.close() }
  })

  it('S1-42 reports failed polling and clears the retry state after transcript recovery', async () => {
    const actual = await vi.importActual('./converse.js')
    vi.stubGlobal('EventSource', undefined)
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue({ status: 200, ok: true, json: async () => ({ events: [] }) })
    vi.stubGlobal('fetch', fetcher)
    const onConnectionState = vi.fn()
    const stream = actual.openStream('session', 0, { onConnectionState })
    try {
      await stream.reconcile()
      expect(onConnectionState).toHaveBeenCalledWith('retrying')
      await stream.reconcile()
      expect(onConnectionState).toHaveBeenLastCalledWith('connected')
    } finally { stream.close() }
  })

  it('S1-42 bounds a stalled transcript request and exposes retry state', async () => {
    const actual = await vi.importActual('./converse.js')
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', undefined)
    let signal
    vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => {
      signal = options.signal
      signal.addEventListener('abort', () => reject(new Error('aborted')))
    })))
    const onConnectionState = vi.fn()
    const stream = actual.openStream('session', 0, { onConnectionState })
    try {
      await vi.advanceTimersByTimeAsync(8000)
      expect(signal.aborted).toBe(true)
      expect(onConnectionState).toHaveBeenCalledWith('retrying')
    } finally { stream.close() }
  })

  it('S1-44 keeps unreadable image errors with removable attachments', async () => {
    imageUrls()
    vi.stubGlobal('FileReader', class { readAsDataURL() { this.onerror() } })
    const input = await setup()
    paste(input)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    expect(screen.getByRole('alert')).toHaveTextContent('Remove it and attach it again.')
    expect(screen.getByRole('button', { name: 'Remove image attachment' })).toBeTruthy()
    expect(converse.postMessage).not.toHaveBeenCalled()
    expect(screen.queryByText(/reach the assistant/)).toBeNull()
  })

  it('S1-44 humanizes turn failures and moves technical codes into closed details', async () => {
    await setup()
    await push(event('error', 'failed', { error: { error_code: 'internal_failure', message: 'POST /api/sessions -> 500' } }))
    expect(screen.getByText('The service had a temporary problem. Try again.')).toBeTruthy()
    const code = screen.getByText('internal_failure')
    expect(code.closest('details')).not.toHaveAttribute('open')
    expect(screen.queryByText(/POST \/api/)).toBeNull()
  })

  it('S1-44 explains the reply length limit without technical wording', async () => {
    await setup()
    await push(event('turn_complete', 'long', { stop_reason: 'cap_hit' }))
    expect(screen.getByText('The reply reached its length limit. Ask the assistant to continue.')).toBeTruthy()
  })

  it('S1-133 gives an actionable assistant-unavailable message', () => {
    expect(agentBannerFor({ errorCode: 'llm_rate_limited' }).message).toBe(
      'The assistant is unavailable. You can still run your existing tools. Try chat again shortly.',
    )
  })
})
