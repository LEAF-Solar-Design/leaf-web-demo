// @vitest-environment jsdom
import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CockpitTopBand, { RIBBON_TABS } from './CockpitTopBand.jsx'
import DraftingRibbon from './DraftingRibbon.jsx'
import { REASONS, reasonCode } from '../lib/actionRegistry.js'

afterEach(cleanup)

describe('CockpitTopBand panel focus', () => {
  it('preserves the default DOM byte for byte', () => {
    const { container } = render(<CockpitTopBand />)
    const tabs = [
      '<button type="button" role="tab" id="ribbon-tab-draw" aria-selected="true" aria-controls="drafting-ribbon" tabindex="0" title="Draw" aria-label="Draw">Draw</button>',
      '<button type="button" role="tab" id="ribbon-tab-model" aria-selected="false" aria-controls="drafting-ribbon" tabindex="-1" disabled="" title="Model: 3D modelling is not in this engine yet" aria-label="Model (unavailable: 3D modelling is not in this engine yet)">Model</button>',
      '<button type="button" role="tab" id="ribbon-tab-insert" aria-selected="false" aria-controls="drafting-ribbon" tabindex="-1" title="Insert" aria-label="Insert">Insert</button>',
      '<button type="button" role="tab" id="ribbon-tab-annotate" aria-selected="false" aria-controls="drafting-ribbon" tabindex="-1" title="Annotate" aria-label="Annotate">Annotate</button>',
      '<button type="button" role="tab" id="ribbon-tab-view" aria-selected="false" aria-controls="drafting-ribbon" tabindex="-1" title="View" aria-label="View">View</button>',
      '<button type="button" role="tab" id="ribbon-tab-manage" aria-selected="false" aria-controls="drafting-ribbon" tabindex="-1" title="Manage" aria-label="Manage">Manage</button>',
    ].join('')
    expect(container.innerHTML).toBe('<div class="cockpit-band" data-testid="cockpit-band"><div class="cockpit-quick" role="toolbar" aria-label="Quick access"><span id="cockpit-quick-file" class="cockpit-quick-slot"></span></div><div class="cockpit-ribbon-tabs" role="tablist" aria-label="Ribbon">' + tabs + '</div></div>')
  })

  it('renders custom tabs with data attributes, reasons and roving activation', () => {
    const onSelect = vi.fn()
    render(<CockpitTopBand selected="project" onSelect={onSelect} tabs={[
      { id: 'project', label: 'Project' },
      { id: 'tools', label: 'Tools' },
      { id: 'activity', label: 'Activity', reason: 'No activity in this session' },
    ]} />)
    expect(screen.getAllByRole('tab').map((tab) => tab.dataset.tab)).toEqual(['project', 'tools', 'activity'])
    const project = screen.getByRole('tab', { name: 'Project' })
    expect(project.getAttribute('aria-selected')).toBe('true')
    expect(project.tabIndex).toBe(0)
    project.focus()
    fireEvent.keyDown(project, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Tools' }))
    expect(onSelect).toHaveBeenCalledWith('tools')
    const activity = screen.getByRole('tab', { name: 'Activity (unavailable: No activity in this session)' })
    expect(activity.disabled).toBe(true)
    expect(activity.title).toBe('Activity: No activity in this session')
    fireEvent.click(activity)
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('selects the first enabled custom tab and reports the fallback once', () => {
    const onSelect = vi.fn()
    const tabs = [{ id: 'off', label: 'Unavailable', reason: 'No tools' }, { id: 'solar', label: 'Solar' }, { id: 'view', label: 'View' }]
    const band = () => <StrictMode><CockpitTopBand tabs={[...tabs]} selected="draw" onSelect={(id) => onSelect(id)} /></StrictMode>
    const { rerender } = render(band())
    const solar = screen.getByRole('tab', { name: 'Solar' })
    expect(solar.getAttribute('aria-selected')).toBe('true')
    expect(solar.tabIndex).toBe(0)
    expect(screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)).toEqual([solar])
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('solar')
    rerender(band())
    expect(onSelect).toHaveBeenCalledTimes(1)
    rerender(<CockpitTopBand tabs={tabs} selected="solar" onSelect={onSelect} />)
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('supports custom tabs through the existing tab and onTab props', () => {
    const onTab = vi.fn()
    render(<CockpitTopBand tabs={[{ id: 'ship', label: 'Ship' }]} tab="draw" onTab={onTab} />)
    expect(screen.getByRole('tab', { name: 'Ship' }).getAttribute('aria-selected')).toBe('true')
    expect(onTab).toHaveBeenCalledTimes(1)
    expect(onTab).toHaveBeenCalledWith('ship')
  })

  it('does not report a fallback when there is no enabled tab', () => {
    const onSelect = vi.fn()
    const { rerender } = render(<CockpitTopBand tabs={[]} selected="missing" onSelect={onSelect} />)
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
    rerender(<CockpitTopBand tabs={[{ id: 'off', label: 'Unavailable', reason: 'No tools' }]} selected="missing" onSelect={onSelect} />)
    expect(screen.getByRole('tab').tabIndex).toBe(-1)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('exposes a local code only for a disabled quick tool with a registered reason', () => {
    const reason = REASONS['unsavedEngineEdits']
    render(<CockpitTopBand before={[
      { id: 'save', label: 'Save', disabled: true, reason },
      { id: 'open', label: 'Open', reason },
    ]} />)
    expect(screen.getByRole('button', { name: /Save \(unavailable/ }).getAttribute('data-reason-code')).toBe(reasonCode(reason))
    expect(screen.getByRole('button', { name: 'Open' }).hasAttribute('data-reason-code')).toBe(false)
  })
  it.each(RIBBON_TABS.filter((tab) => !tab.reason))('$label: Tab skips CSS-hidden controls and enters the first visible enabled control', ({ id, label }) => {
    render(<>
      <CockpitTopBand tab={id} />
      <button>Details</button>
      <DraftingRibbon tab={id}>
        <div style={{ display: 'none' }}><button className="ribbon-tool">Hidden by display</button></div>
        <button className="ribbon-tool" style={{ visibility: 'hidden' }}>Hidden by visibility</button>
        <button className="ribbon-tool" disabled>Unavailable</button>
        <button className="ribbon-tool">Visible command</button>
      </DraftingRibbon>
    </>)
    const tab = screen.getByRole('tab', { name: label, exact: true })
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Visible command' }))
  })

  it.each(RIBBON_TABS.filter((tab) => !tab.reason))('$label: Tab reaches More panels with no expanded panel', ({ id, label }) => {
    render(<><CockpitTopBand tab={id} /><DraftingRibbon tab={id} visiblePanelCount={0} clusters={[
      { id: 'panel', label: 'Panel', tools: [{ id: 'command', label: 'Command' }] },
    ]} /></>)
    const tab = screen.getByRole('tab', { name: label, exact: true })
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'More panels' }))
  })

  it('Annotate with only unavailable tools focuses the panel itself', () => {
    render(<><CockpitTopBand tab="annotate" /><button>Details</button><DraftingRibbon tab="annotate" clusters={[
      { id: 'annotation', label: 'Annotation', tools: [{ id: 'text', label: 'Text', disabled: true, reason: 'not in the browser engine yet' }] },
    ]} /></>)
    const tab = screen.getByRole('tab', { name: 'Annotate' })
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('toolbar', { name: 'Drafting tools' }))
    expect(screen.getByRole('button', { name: /Text/ }).disabled).toBe(true)
  })

  it('Tab from Draw skips a disabled tool and the intervening Details chip', () => {
    render(<>
      <CockpitTopBand />
      <button>Details</button>
      <DraftingRibbon clusters={[{ id: 'draw', label: 'Draw tools', tools: [
        { id: 'off', label: 'Unavailable', disabled: true, reason: 'No drawing' },
        { id: 'line', label: 'Line' },
      ] }]} />
    </>)
    const draw = screen.getByRole('tab', { name: 'Draw' })
    draw.focus()
    fireEvent.keyDown(draw, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Line' }))
    expect(screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)).toEqual([draw])
  })

  it('Tab from Draw skips a disabled first child with tabIndex zero', () => {
    render(<>
      <CockpitTopBand />
      <div id="drafting-ribbon" tabIndex={-1}>
        <button disabled tabIndex={0}>Unavailable</button>
        <span role="button" aria-disabled="true" tabIndex={0}>Unavailable custom control</span>
        <button>Enabled command</button>
      </div>
    </>)
    const draw = screen.getByRole('tab', { name: 'Draw' })
    draw.focus()
    fireEvent.keyDown(draw, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Enabled command' }))
  })

  it('Tab reaches More panels when every panel has collapsed', () => {
    render(<><CockpitTopBand /><DraftingRibbon visiblePanelCount={0} clusters={[
      { id: 'clipboard', label: 'Clipboard', tools: [{ id: 'copy', label: 'Copy' }] },
    ]} /></>)
    const draw = screen.getByRole('tab', { name: 'Draw' })
    draw.focus()
    fireEvent.keyDown(draw, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'More panels' }))
  })

  it('leaves Shift+Tab native and preserves the existing arrow activation pattern', () => {
    const onTab = vi.fn()
    render(<CockpitTopBand tab="insert" onTab={onTab} />)
    const insert = screen.getByRole('tab', { name: 'Insert' })
    insert.focus()
    expect(fireEvent.keyDown(insert, { key: 'Tab', shiftKey: true })).toBe(true)
    fireEvent.keyDown(insert, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Annotate' }))
    expect(onTab).toHaveBeenCalledWith('annotate')
  })
})
