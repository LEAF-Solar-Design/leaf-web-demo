// @vitest-environment jsdom
import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useComposerPlanMode } from '../App.jsx'
import { fetchInlineAuthoringFlags } from '../api.js'
import { createCatalogController } from '../controllers/catalog/createCatalogController.js'
import PromptBox from './PromptBox.jsx'

vi.mock('../api.js', async importOriginal => ({
  ...await importOriginal(),
  fetchInlineAuthoringFlags: vi.fn(),
}))

beforeEach(() => {
  fetchInlineAuthoringFlags.mockReset().mockResolvedValue({ plan_mode: true })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

const prompt = 'count the panels'
function Composer({ sessionId, controller, onDispatch }) {
  const plan = useComposerPlanMode(sessionId, true, controller)
  return <PromptBox {...plan} sessionId={sessionId} value={prompt} onChange={() => {}}
    onDispatch={onDispatch} mcpDiscoveryEnabled={false} />
}

function mount(sessionId = 'one', controller = { setContext: vi.fn() }, onDispatch = vi.fn()) {
  const composer = id => <StrictMode><Composer sessionId={id} controller={controller} onDispatch={onDispatch} /></StrictMode>
  const view = render(composer(sessionId))
  return { ...view, switchTo: id => view.rerender(composer(id)) }
}

const findPlan = () => screen.findByRole('button', { name: 'Plan' })
const input = () => screen.getByRole('combobox', { name: 'Command bar' })

it('toggles with click and Shift+Tab and restores the choice per conversation', async () => {
  const view = mount()
  const pill = await findPlan()
  expect(pill.parentElement.firstElementChild).toBe(pill)
  expect(pill).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(pill)
  view.switchTo('two')
  expect(pill).toHaveAttribute('aria-pressed', 'false')
  view.switchTo('one')
  expect(pill).toHaveAttribute('aria-pressed', 'true')
  expect(fetchInlineAuthoringFlags).toHaveBeenCalledTimes(1)
  view.unmount()

  mount('one')
  expect(await findPlan()).toHaveAttribute('aria-pressed', 'true')
  act(() => input().focus())
  expect(fireEvent.keyDown(input(), { key: 'Tab', shiftKey: true })).toBe(false)
  expect(input()).toHaveFocus()
  expect(await findPlan()).toHaveAttribute('aria-pressed', 'false')
  expect(fetchInlineAuthoringFlags).toHaveBeenCalledTimes(2)
})

it('retains the draft choice when the first send acquires a conversation id', async () => {
  const view = mount(null)
  const pill = await findPlan()
  fireEvent.click(pill)
  view.switchTo('newly-created')
  expect(pill).toHaveAttribute('aria-pressed', 'true')
  view.unmount()
  mount('newly-created')
  expect(await findPlan()).toHaveAttribute('aria-pressed', 'true')
})

it('starts off and remains usable when browser storage is unavailable', async () => {
  const view = mount()
  const pill = await findPlan()
  const unavailable = () => { throw new Error('Storage unavailable') }
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(unavailable)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(unavailable)
  view.switchTo('private')
  expect(pill).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(pill)
  expect(pill).toHaveAttribute('aria-pressed', 'true')
  view.switchTo('another')
  expect(pill).toHaveAttribute('aria-pressed', 'false')
})

it.each(['disabled', 'error'])('leaves the pill hidden and Shift+Tab available when the flag read is %s', async outcome => {
  if (outcome === 'error') fetchInlineAuthoringFlags.mockRejectedValue(new Error('offline'))
  else fetchInlineAuthoringFlags.mockResolvedValue({ plan_mode: false })
  localStorage.setItem('leaf.planMode.one', '1')
  const controller = { setContext: vi.fn() }
  mount('one', controller)
  await act(async () => {})
  expect(fetchInlineAuthoringFlags).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: 'Plan' })).toBeNull()
  expect(fireEvent.keyDown(input(), { key: 'Tab', shiftKey: true })).toBe(true)
  expect(controller.setContext).not.toHaveBeenCalled()
})

it('uses catalog context to route a Plan send and restores ordinary routing on toggle-off', async () => {
  const decision = { lane: 'run', tool: 'count-modules', confidence: 0.97 }
  const routePrompt = vi.fn(async () => decision)
  const startAgentTurn = vi.fn()
  const commitDecision = vi.fn(value => value)
  const catalog = createCatalogController({
    services: { getTools: vi.fn(), getCapabilities: vi.fn(), routePrompt },
    adapters: { startAgentTurn, commitDecision, draftStorage: null },
    context: { agentDisabled: false },
  })
  const controller = { setContext: vi.fn(catalog.setContext) }
  const send = vi.fn((override, options) => catalog.actions.dispatch(override ?? prompt, options))
  mount('one', controller, send)
  const pill = await findPlan()
  expect(controller.setContext).not.toHaveBeenCalled()
  fireEvent.click(pill)
  expect(controller.setContext).toHaveBeenLastCalledWith({ planMode: true })
  fireEvent.keyDown(input(), { key: 'Enter' })
  await waitFor(() => expect(startAgentTurn).toHaveBeenCalledOnce())
  expect(send.mock.calls[0][1]).not.toHaveProperty('planMode')
  expect(routePrompt.mock.calls[0][3]).toEqual({ allowSecretOnce: false })
  expect(startAgentTurn.mock.calls[0][2]).toEqual({ allowSecretOnce: false })
  expect(commitDecision).not.toHaveBeenCalled()
  fireEvent.keyDown(input(), { key: 'Tab', shiftKey: true })
  fireEvent.keyDown(input(), { key: 'Enter' })
  await waitFor(() => expect(commitDecision).toHaveBeenCalledWith(decision))
  expect(startAgentTurn).toHaveBeenCalledOnce()
})
