import React, { useRef, useState } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { profileRibbonTabs } from '../lib/ribbonClusters.js'
import SolarFlowSeat from './SolarFlowSeat.jsx'
import SolarToolForm from './SolarToolForm.jsx'
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
// onOpenForm sets solarFormTool, which supplies activeForm and the form ref.
function RibbonSeat({ onRun, onSubmit, seated = true }) {
  const [solarFormTool, setSolarFormTool] = useState(null)
  const [otherState, setOtherState] = useState(0)
  const formRef = useRef(null)
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
      <SolarFlowSeat seated={seated} activeForm={solarFormTool} formRef={formRef}>
        <span>Flow rail</span>
        {solarFormTool && <div ref={formRef} tabIndex={-1} data-testid="ribbon-form-host">
          <SolarToolForm key={solarFormTool.name} tool={solarFormTool}
            onSubmit={onSubmit} onClose={() => setSolarFormTool(null)} />
        </div>}
      </SolarFlowSeat>
    </div>
  </div>
}

const toggle = () => screen.getByRole('button', { name: 'Solar flow tools' })
const content = () => screen.getByTestId('solar-flow-seat-content')
function choose(name) {
  const button = screen.getByRole('button', { name, exact: true })
  expect(button).toBeEnabled()
  fireEvent.click(button)
}
function expectFocusedForm() {
  const host = screen.getByTestId('ribbon-form-host')
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
  expect(screen.queryByTestId('ribbon-form-host')).toBeNull()
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
  expect(screen.queryByTestId('ribbon-form-host')).toBeNull()
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
  expect(screen.queryByTestId('ribbon-form-host')).toBeNull()
  expect(content()).not.toBeVisible()
})

it.each([false, true])('closing via the ribbon restores the prior user preference: expanded=%s', (expanded) => {
  render(<RibbonSeat onRun={vi.fn()} onSubmit={vi.fn()} />)
  if (expanded) fireEvent.click(toggle())
  choose(voltageDrop.name)
  expectFocusedForm()
  choose(voltageDrop.name)
  expect(screen.queryByTestId('ribbon-form-host')).toBeNull()
  expect(toggle()).toHaveAttribute('aria-expanded', String(expanded))
  expect(getComputedStyle(content()).display === 'none').toBe(!expanded)
})

it('choosing another form reveals a manually collapsed seat again and focuses the new form', () => {
  render(<RibbonSeat onRun={vi.fn()} onSubmit={vi.fn()} />)
  choose(voltageDrop.name)
  const oldHost = screen.getByTestId('ribbon-form-host')
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
  expect(screen.queryByTestId('ribbon-form-host')).toBeNull()
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
  function EmptyForm({ activeForm }) {
    const formRef = useRef(null)
    return <SolarFlowSeat seated activeForm={activeForm} formRef={formRef}>
      {activeForm && <div ref={formRef} tabIndex={-1} data-testid="empty-form"><input disabled /></div>}
    </SolarFlowSeat>
  }
  const view = render(<EmptyForm activeForm={null} />)
  view.rerender(<EmptyForm activeForm={rows[0]} />)
  expect(screen.getByTestId('empty-form')).toBeVisible()
  expect(screen.getByTestId('empty-form')).toHaveFocus()
})
