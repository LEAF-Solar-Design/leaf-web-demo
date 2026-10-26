// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { createCatalogController } from '../catalog/createCatalogController.js'
import usePhoneDrawerEscape from '../../lib/usePhoneDrawerEscape.js'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))
afterEach(cleanup)

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const decision = { lane: 'run', tool: 'count', confidence: 0.9, params: {} }
function setup(context = {}) {
  const pending = deferred()
  const commitDecision = vi.fn((value) => value)
  const startAgentTurn = vi.fn().mockResolvedValue(undefined)
  const routePrompt = vi.fn(() => pending.promise)
  const controller = createCatalogController({
    services: {
      getTools: async () => [], getCapabilities: async () => ({ families: [] }),
      routePrompt,
    },
    adapters: { commitDecision, startAgentTurn, draftStorage: null },
    context,
  })
  return { controller, pending, commitDecision, startAgentTurn, routePrompt }
}

it('discards a route resolved after the bar is reset', async () => {
  const { controller, pending, commitDecision } = setup()
  const work = controller.actions.dispatch('count panels')
  controller.actions.resetTransient()
  pending.resolve(decision)
  await work
  expect(commitDecision).not.toHaveBeenCalled()
  expect(controller.getState().route).toBeNull()
})

it('discards a route when the drawing changes', async () => {
  const { controller, pending, commitDecision } = setup({ drawingId: 'first' })
  const work = controller.actions.dispatch('count panels')
  controller.setContext({ drawingId: 'second' })
  pending.resolve(decision)
  await work
  expect(commitDecision).not.toHaveBeenCalled()
  expect(controller.getState().routing).toBe(false)
})

it('does not launch an agent after switching modes', async () => {
  const { controller, pending, startAgentTurn } = setup({ agentDisabled: false })
  const work = controller.actions.dispatch('help with panels')
  controller.setContext({ mock: true })
  pending.resolve({ lane: 'build', confidence: 0.3 })
  await work
  expect(startAgentTurn).not.toHaveBeenCalled()
})

it('discards failures from the previous project', async () => {
  const { controller, pending } = setup({ projectId: 'first' })
  const work = controller.actions.dispatch('count panels')
  controller.setContext({ projectId: 'second' })
  pending.reject(new Error('Old request failed'))
  await work
  expect(controller.getState().routeError).toBeNull()
})

it('does not clear a newer dispatch busy state when the old request settles', async () => {
  const { controller, pending, routePrompt } = setup()
  const first = controller.actions.dispatch('count panels')
  controller.actions.resetTransient()
  const newer = deferred()
  routePrompt.mockReturnValue(newer.promise)
  const second = controller.actions.dispatch('count again')
  pending.resolve(decision)
  await first
  expect(controller.getState().routing).toBe(true)
  expect(controller.getState().route).toBeNull()
  newer.resolve(decision)
  await second
  expect(controller.getState().routing).toBe(false)
  expect(controller.getState().route.tool).toBe('count')
})

it('does not publish agent completion after a reset', async () => {
  const agent = deferred()
  const { controller, pending, startAgentTurn } = setup({ agentDisabled: false })
  startAgentTurn.mockReturnValue(agent.promise)
  const work = controller.actions.dispatch('help with panels')
  pending.resolve({ lane: 'build', confidence: 0.3 })
  await Promise.resolve()
  expect(startAgentTurn).toHaveBeenCalledTimes(1)
  controller.actions.resetTransient()
  agent.resolve()
  await work
  expect(controller.getState().agentMode).toBeNull()
})

function DrawerShell() {
  const [studioDrawer, setDrawer] = useState('none')
  usePhoneDrawerEscape({ studioShell: true, phoneViewport: true, studioDrawer }, () => setDrawer('none'))
  return <div className="app" data-studio-shell="cockpit" data-drawer={studioDrawer}>
    <aside className="nav"><h2>Available tools</h2><button>Choose tool</button></aside>
    <div className="rail-stack"><aside className="rail"><h2>Recent jobs</h2></aside>
      <div className="studio-profile-info"><h2>Your plan</h2></div></div>
    <div className="result-block"><h2>Latest result</h2></div>
    <div className="studio-drawer-tabs">
      {['Catalog', 'Jobs', 'Result', 'Plan'].map((label, index) => <button key={label}
        onClick={() => setDrawer(['nav', 'jobs', 'result', 'plan'][index])}>{label}</button>)}
    </div>
  </div>
}

it('moves focus into every opened drawer', () => {
  render(<DrawerShell />)
  for (const [tab, heading] of [['Catalog', 'Available tools'], ['Jobs', 'Recent jobs'], ['Result', 'Latest result'], ['Plan', 'Your plan']]) {
    fireEvent.click(screen.getByRole('button', { name: tab }))
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: heading }))
  }
})

it('returns focus to each corresponding drawer button on Escape', () => {
  render(<DrawerShell />)
  for (const tab of ['Catalog', 'Jobs', 'Result', 'Plan']) {
    const button = screen.getByRole('button', { name: tab })
    fireEvent.click(button)
    fireEvent.keyDown(document.activeElement, { key: 'Escape' })
    expect(document.activeElement).toBe(button)
  }
})
