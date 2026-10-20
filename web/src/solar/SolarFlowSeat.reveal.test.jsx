import React, { useState } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { profileRibbonTabs } from '../lib/ribbonClusters.js'
import SolarFlowSeat from './SolarFlowSeat.jsx'
import SolarToolForm from './SolarToolForm.jsx'
import SolarStepEditor from './SolarStepEditor.jsx'
import SolarSettingsForm from './SolarSettingsForm.jsx'
import presets from '../../../server/solar_tools/solar_design_presets.json'
import presetList from '../../../server/solar_tools/solar_design_presets_list.json'
import voltageDrop from '../../../server/solar_tools/solar_nec_ac_voltage_drop.json'
import ampacity from '../../../server/solar_tools/solar_nec_ampacity_correction.json'
import conduitFill from '../../../server/solar_tools/solar_nec_conduit_fill.json'
import feeder from '../../../server/solar_tools/solar_nec_feeder_ocpd.json'
import tracking from '../../../server/solar_tools/solar_solaredge_tracking_read.json'
import trench from '../../../server/solar_tools/solar_trench_manual.json'
import unitSync from '../../../server/solar_tools/solar_unit_sync.json'

const declarations = [presets, presetList, voltageDrop, ampacity, conduitFill, feeder, tracking, trench, unitSync]
const rows = declarations.map((declaration) => ({
  ...declaration.record,
  solar: {
    schema: 'leaf.solar-tool-view.v1', name: declaration.name, family: declaration.family,
    wave: declaration.wave, order: declaration.order,
    entitlement: declaration.entitlement, interaction: declaration.interaction,
  },
  availability: { entitled: true, implemented: true, engine_ready: true, input_ready: true, refusal_reasons: [] },
}))
const runOnly = {
  ...rows[2], name: 'solar-run-only',
  solar: { ...rows[2].solar, name: 'solar-run-only', interaction: { mode: 'none' } },
}
const families = [...new Set(rows.map((row) => row.solar.family))].map((id) => ({
  family_id: id, label: id,
  capabilities: [...rows, runOnly].filter((row) => row.solar.family === id),
}))

let collapseStyle
beforeEach(() => {
  // jsdom does not evaluate the desktop media query. Use its actual production
  // collapse rule outside that query so visibility assertions exercise the CSS.
  // Verification runs from web/; jsdom's import.meta.url need not be a file URL.
  const css = readFileSync(resolve(process.cwd(), 'src/site/cockpit.css'), 'utf8')
  const rule = css.split('\n').find((line) => line.includes('[data-expanded="false"] .solar-flow-seat-content'))
  expect(rule).toBeTruthy()
  collapseStyle = document.createElement('style')
  collapseStyle.textContent = rule
  document.head.appendChild(collapseStyle)
})
afterEach(() => {
  cleanup()
  collapseStyle?.remove()
})

// Exercise the real ribbon projection and form router at App's state boundary:
// onOpenForm sets solarFormTool, which mounts the form in App's Fragment.
function RibbonSeat({ onRun, onSubmit, seated = true }) {
  const [solarFormTool, setSolarFormTool] = useState(null)
  const [otherState, setOtherState] = useState(0)
  const tabs = profileRibbonTabs('solar', {
    families, onRun,
    solarRail: { families, openName: solarFormTool?.name ?? null, onOpenForm: setSolarFormTool },
  })
  const tools = tabs.flatMap((tab) => tab.clusters.flatMap((cluster) => cluster.tools ?? []))
    .filter((tool) => [...rows, runOnly].some((row) => row.name === tool.id))
  return <div className="studio-shell">
    <div className="app" data-surface="solar">
      <div role="toolbar" aria-label="Solar ribbon">
        {tools.map((tool) => <button key={tool.id} type="button" disabled={tool.disabled}
          data-tool={tool.id} aria-expanded={tool.expanded} onClick={tool.onClick}>{tool.id}</button>)}
      </div>
      <button type="button" onClick={() => setOtherState((value) => value + 1)}>Other action {otherState}</button>
      <SolarFlowSeat seated={seated}>
        <span>Flow rail</span>
        <>
          {solarFormTool && <SolarToolForm key={solarFormTool.name} tool={solarFormTool}
            onSubmit={onSubmit} onClose={() => setSolarFormTool(null)} />}
        </>
      </SolarFlowSeat>
    </div>
  </div>
}

const toggle = () => screen.getByRole('button', { name: 'Solar flow tools' })
const content = () => screen.getByTestId('solar-flow-seat-content')
const formHost = () => document.getElementById('solar-tool-form')
function choose(name) {
  const button = screen.getByRole('button', { name, exact: true })
  expect(button).toBeEnabled()
  fireEvent.click(button)
}
function expectFocusedForm() {
  const host = formHost()
  expect(host).toBeVisible()
  expect(host.contains(document.activeElement)).toBe(true)
  expect(document.activeElement).toBeVisible()
  expect(document.activeElement).not.toBeDisabled()
}

it.each(rows.map((row) => [row.name]))('%s reveals and focuses its form through the ribbon without dispatching', (name) => {
  const onRun = vi.fn()
  const onSubmit = vi.fn()
  render(<RibbonSeat onRun={onRun} onSubmit={onSubmit} />)
  expect(toggle()).toHaveAttribute('aria-expanded', 'false')
  expect(content()).not.toBeVisible()
  expect(getComputedStyle(content()).display).toBe('none')

  choose(name)
  expect(toggle()).toHaveAttribute('aria-expanded', 'true')
  expect(getComputedStyle(content()).display).not.toBe('none')
  expectFocusedForm()
  expect(screen.getByRole('button', { name: /Review & run/i })).toBeVisible()

  // The seat toggle stays authoritative even while a form is active.
  fireEvent.click(toggle())
  expect(content()).not.toBeVisible()
  fireEvent.click(toggle())
  expect(content()).toBeVisible()
  expectFocusedForm()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
  expect(formHost()).toBeNull()
  // The last explicit toggle expanded the seat, so cancel keeps it expanded.
  expect(toggle()).toHaveAttribute('aria-expanded', 'true')
  expect(onRun).not.toHaveBeenCalled()
  expect(onSubmit).not.toHaveBeenCalled()
})

it('cancel restores the initially collapsed preference after a temporary reveal', () => {
  const onRun = vi.fn()
  const onSubmit = vi.fn()
  render(<RibbonSeat onRun={onRun} onSubmit={onSubmit} />)
  choose(voltageDrop.name)
  expectFocusedForm()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
  expect(formHost()).toBeNull()
  expect(toggle()).toHaveAttribute('aria-expanded', 'false')
  expect(content()).not.toBeVisible()
  expect(onRun).not.toHaveBeenCalled()
  expect(onSubmit).not.toHaveBeenCalled()
})

it('only explicit Review hands the form values to the existing submit callback', () => {
  const onRun = vi.fn()
  const onSubmit = vi.fn()
  render(<RibbonSeat onRun={onRun} onSubmit={onSubmit} />)
  choose(presetList.name)
  fireEvent.change(screen.getByRole('textbox', { name: 'Drawing id' }), { target: { value: 'drawing-1' } })
  expect(onRun).not.toHaveBeenCalled()
  expect(onSubmit).not.toHaveBeenCalled()
  const review = screen.getByRole('button', { name: /Review & run/i })
  expect(review).toBeEnabled()
  fireEvent.click(review)
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(rows.find((row) => row.name === presetList.name), { drawing_id: 'drawing-1' })
  expect(onRun).not.toHaveBeenCalled()
  expect(formHost()).toBeNull()
  expect(content()).not.toBeVisible()
})

it.each([false, true])('closing via the ribbon restores the prior user preference: expanded=%s', (expanded) => {
  render(<RibbonSeat onRun={vi.fn()} onSubmit={vi.fn()} />)
  if (expanded) fireEvent.click(toggle())
  choose(voltageDrop.name)
  expectFocusedForm()
  choose(voltageDrop.name)
  expect(formHost()).toBeNull()
  expect(toggle()).toHaveAttribute('aria-expanded', String(expanded))
  expect(getComputedStyle(content()).display === 'none').toBe(!expanded)
})

it('choosing another form reveals a manually collapsed seat again and focuses the new form', () => {
  render(<RibbonSeat onRun={vi.fn()} onSubmit={vi.fn()} />)
  choose(voltageDrop.name)
  const oldHost = formHost()
  const oldControl = document.activeElement
  fireEvent.click(toggle())
  expect(oldHost).not.toBeVisible()
  choose(conduitFill.name)
  expectFocusedForm()
  expect(document.activeElement).not.toBe(oldControl)
  choose(conduitFill.name)
  expect(content()).not.toBeVisible()
})

it('load, unrelated state updates, and run-mode ribbon tools do not reveal the seat', () => {
  const onRun = vi.fn()
  const onSubmit = vi.fn()
  const view = render(<RibbonSeat onRun={onRun} onSubmit={onSubmit} />)
  expect(content()).not.toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Other action 0' }))
  expect(content()).not.toBeVisible()
  choose(runOnly.name)
  expect(onRun).toHaveBeenCalledTimes(1)
  expect(onRun).toHaveBeenCalledWith(runOnly)
  expect(content()).not.toBeVisible()
  view.rerender(<RibbonSeat onRun={onRun} onSubmit={onSubmit} />)
  expect(content()).not.toBeVisible()
  expect(formHost()).toBeNull()
  expect(onSubmit).not.toHaveBeenCalled()
})

it('unseated forms remain visible and receive focus without adding a seat toggle', () => {
  render(<RibbonSeat seated={false} onRun={vi.fn()} onSubmit={vi.fn()} />)
  expect(screen.queryByTestId('solar-flow-seat')).toBeNull()
  choose(voltageDrop.name)
  expectFocusedForm()
  expect(screen.queryByRole('button', { name: 'Solar flow tools' })).toBeNull()
})

it('focus falls back to the form host when it has no enabled controls', () => {
  render(<RibbonSeat onRun={vi.fn()} onSubmit={vi.fn()} />)
  choose(voltageDrop.name)
  for (const control of formHost().querySelectorAll('input, select, textarea, button')) control.disabled = true
  fireEvent.click(toggle())
  fireEvent.click(toggle())
  expect(formHost()).toBeVisible()
  expect(formHost()).toHaveFocus()
})

it.each(['step', 'settings'])('reveals and focuses App\'s existing %s ribbon form shape', (kind) => {
  const onSubmit = vi.fn()
  const pendingRead = () => new Promise(() => {})
  function Seat({ active }) {
    return <div className="studio-shell"><div className="app" data-surface="solar"><SolarFlowSeat seated><>
      {active && (kind === 'step'
        ? <SolarStepEditor key={rows[2].name} row={rows[2]} onSubmit={onSubmit} onClose={() => {}} />
        : <div id="solar-tool-form" key="solar-settings">
          <SolarSettingsForm context={{}} readIntake={pendingRead} readVersions={pendingRead}
            onSubmit={onSubmit} onClose={() => {}} />
        </div>)}
    </></SolarFlowSeat></div></div>
  }
  const view = render(<Seat active={false} />)
  expect(content()).not.toBeVisible()
  view.rerender(<Seat active />)
  const host = document.getElementById(kind === 'step' ? 'solar-step-editor' : 'solar-tool-form')
  expect(host).toBeVisible()
  expect(host.contains(document.activeElement)).toBe(true)
  expect(document.activeElement).toBeVisible()
  expect(onSubmit).not.toHaveBeenCalled()
  view.rerender(<Seat active={false} />)
  expect(content()).not.toBeVisible()
})

it.each(['flow', 'project'])('mounting a form inside the %s host never reveals the seat', (kind) => {
  function Seat({ active }) {
    return <div className="studio-shell"><div className="app" data-surface="solar"><SolarFlowSeat seated><>
      <div className={kind === 'flow' ? 'solar-flow-host' : undefined}
        id={kind === 'project' ? 'solar-tool-form' : undefined}>
        {active && <SolarStepEditor row={rows[2]} onSubmit={vi.fn()} onClose={() => {}} />}
      </div>
    </></SolarFlowSeat></div></div>
  }
  const view = render(<Seat active={false} />)
  view.rerender(<Seat active />)
  expect(toggle()).toHaveAttribute('aria-expanded', 'false')
  expect(content()).not.toBeVisible()
  fireEvent.click(toggle())
  expect(content()).toBeVisible()
})
