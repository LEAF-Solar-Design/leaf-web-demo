import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(), resolveApproval: vi.fn(), cancelTurn: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  classifyAgentError: vi.fn(() => 'busy'),
}))

import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'

const first = { project_id: '11111111-1111-4111-8111-111111111111', input_version_id: '22222222-2222-4222-8222-222222222222' }
const second = { ...first, input_version_id: '33333333-3333-4333-8333-333333333333' }
const panel = (projectContext) => <ConversePanel sessionId="same-session" projectContext={projectContext} />
const reply = () => screen.getByRole('textbox', { name: /reply to the assistant/i })
const send = (text) => {
  fireEvent.change(reply(), { target: { value: text } })
  fireEvent.keyDown(reply(), { key: 'Enter' })
}

beforeEach(() => {
  vi.clearAllMocks()
  converse.postMessage.mockResolvedValue({ status: 'started', turn_id: 'sent' })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('direct replies send the selected project/version without reopening the session', async () => {
  const view = render(panel(first))
  view.rerender(panel(second))
  send('read this selected version')
  await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1))
  expect(converse.postMessage.mock.calls[0]).toEqual(['same-session', expect.objectContaining({ projectContext: second })])
  expect(converse.openStream).toHaveBeenCalledTimes(1)
})

it('an in-flight busy retry retains the original selected version', async () => {
  let rejectFirst
  converse.postMessage.mockImplementationOnce(() => new Promise((resolve, reject) => { rejectFirst = reject }))
    .mockResolvedValueOnce({ status: 'queued', queued_id: 'queued' })
  const view = render(panel(first))
  send('read the original selection')
  await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1))
  view.rerender(panel(second))
  await act(async () => { rejectFirst(new Error('turn busy')) })
  await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(2))
  expect(converse.postMessage.mock.calls[0][1].projectContext).toEqual(first)
  expect(converse.postMessage.mock.calls[1][1]).toMatchObject({ queue: true, projectContext: first })
})

it('image-only requests capture selection before asynchronous image reading', async () => {
  let reader
  vi.stubGlobal('FileReader', class {
    readAsDataURL() { reader = this }
  })
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:synthetic'), revokeObjectURL: vi.fn() })
  const view = render(panel(first))
  const file = new File([new Uint8Array(10)], 'roof.png', { type: 'image/png' })
  fireEvent.paste(reply(), { clipboardData: { items: [{ kind: 'file', type: file.type, getAsFile: () => file }] } })
  fireEvent.click(screen.getByRole('button', { name: /^send$/i }))
  await waitFor(() => expect(reader).toBeDefined())
  view.rerender(panel(second))
  await act(async () => { reader.result = 'data:image/png;base64,c2FtcGxl'; reader.onload() })
  await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1))
  expect(converse.postMessage.mock.calls[0][1]).toMatchObject({
    projectContext: first, images: [{ media_type: 'image/png', data: 'c2FtcGxl' }],
  })
  expect(converse.postMessage.mock.calls[0][1]).not.toHaveProperty('text')
})

it('ordinary unselected replies omit the pin', async () => {
  render(panel(null))
  send('discuss this project')
  await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(1))
  expect(converse.postMessage.mock.calls[0][1]).not.toHaveProperty('projectContext')
})
