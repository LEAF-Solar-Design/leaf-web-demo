// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CockpitTopBand, { RIBBON_TABS } from './CockpitTopBand.jsx'
import DraftingRibbon from './DraftingRibbon.jsx'

afterEach(cleanup)

describe('CockpitTopBand panel focus', () => {
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
