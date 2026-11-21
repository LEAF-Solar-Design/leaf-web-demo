import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { track } from '../../telemetry.js'
import PlanCard from './PlanCard.jsx'

vi.mock('../../telemetry.js', async original => ({ ...await original(), track: vi.fn(), trackErrorShown: vi.fn() }))

const plan = { title: 'Label the roof', target: 'branch', change_kind: 'revise', target_tool_name: 'roof-label',
  summary: '<img src=x>', behavior: ['Find roof panels', 'Label matching panels'],
  inputs: [{ name: 'threshold', type: 'number', default: 0, description: 'Minimum area' }],
  output: 'A count', capabilities: ['drawing.write'], undo: 'Use Undo',
  assumptions: ['A drawing is open'], acceptance: ['Every panel has a label'],
  answers: { q1: { labels: ['Selected'] }, q2: { other: 'Roof layer' } } }
const props = { sessionId: 'session', proposal: { confirmation_id: 'plan', revision: 2, plan } }
const response = (status, data) => ({ status, ok: status < 300, json: async () => data })
const button = name => screen.getByRole('button', { name })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); vi.clearAllMocks() })

it('renders the plan contract as plain text and reports the replaced revision', () => {
  const view = render(<PlanCard {...props} />)
  for (const text of ['Label the roof', 'Branch tool, runs inside AutoCAD', 'Revise roof-label', '<img src=x>',
    'A count', 'Changes your drawing', 'Use Undo', 'A drawing is open', 'Every panel has a label', 'q1 · Selected', 'q2 · Roof layer']) {
    expect(screen.getByText(text)).toBeVisible()
  }
  expect(screen.getByText('Find roof panels').parentElement.tagName).toBe('OL')
  expect(screen.getByRole('table')).toHaveTextContent('thresholdnumber0Minimum area')
  expect(document.querySelector('img')).toBeNull()
  view.rerender(<PlanCard {...props} replacedBy={3} />)
  expect(screen.getByRole('status')).toHaveTextContent('Replaced by revision 3')
  expect(screen.queryByRole('button')).toBeNull()
})

it.each([
  ['Approve and build', true, undefined, 'plan.approved'],
  ['Keep planning', false, 'Use a table', 'plan.revised'],
  ['Cancel', false, undefined, 'plan.cancelled'],
])('%s records a decision and then posts one confirm turn', async (label, approved, feedback, event) => {
  const fetch = vi.fn().mockResolvedValueOnce(response(200, { resolved: true }))
    .mockResolvedValueOnce(response(202, { turn_id: 'next' }))
  vi.stubGlobal('fetch', fetch)
  render(<PlanCard {...props} />)
  const action = button(label)
  action.focus()
  expect(action).toHaveFocus()
  await act(async () => action.click())
  if (feedback) {
    expect(fetch).not.toHaveBeenCalled()
    const box = screen.getByRole('textbox', { name: /What should change/ })
    expect(box.maxLength).toBe(1000)
    expect(button('Send feedback')).toBeDisabled()
    fireEvent.change(box, { target: { value: feedback } })
    await act(async () => fireEvent.click(button('Send feedback')))
  }
  expect(fetch.mock.calls.map(([url, opts]) => [new URL(url, 'http://test').pathname, JSON.parse(opts.body)]))
    .toEqual([['/api/agent/approvals/plan', { approved }], ['/api/sessions/session/messages',
      { confirm: { confirmationId: 'plan', approved, ...(feedback ? { feedback } : {}) } }]])
  expect(track.mock.calls).toContainEqual(approved ? [event, { revision: 2 }] : [event])
  expect(screen.queryByRole('button')).toBeNull()
})

it('retries only the missing confirm turn with the original feedback', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(response(200, { resolved: true }))
    .mockRejectedValueOnce(new Error()).mockResolvedValueOnce(response(202, { turn_id: 'next' }))
  vi.stubGlobal('fetch', fetch)
  render(<PlanCard {...props} />)
  fireEvent.click(button('Keep planning'))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Use green labels' } })
  await act(async () => fireEvent.click(button('Send feedback')))
  await act(async () => fireEvent.click(button('Resume denial')))
  expect(fetch.mock.calls[2][0]).toBe(fetch.mock.calls[1][0])
  expect(fetch.mock.calls[2][1].body).toBe(fetch.mock.calls[1][1].body)
  expect(screen.queryByRole('button')).toBeNull()
})

it('expires an outstanding plan while it is visible', () => {
  vi.useFakeTimers()
  render(<PlanCard {...props} expiresAt={Date.now() + 1000} />)
  expect(button('Approve and build')).toBeEnabled()
  act(() => vi.advanceTimersByTime(1000))
  expect(screen.getByRole('status')).toHaveTextContent('Expired, ask to re-propose')
  expect(screen.queryByRole('button')).toBeNull()
})

it('checks freeform feedback before recording a denial', async () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  render(<PlanCard {...props} />)
  fireEvent.click(button('Keep planning'))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'api_key: ' + 'x'.repeat(24) } })
  await act(async () => fireEvent.click(button('Send feedback')))
  expect(screen.getByRole('alert')).toBeVisible()
  expect(fetch).not.toHaveBeenCalled()
  expect(button('Cancel')).toBeEnabled()
})
