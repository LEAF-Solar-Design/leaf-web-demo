// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CockpitTopBand from './CockpitTopBand.jsx'
import DraftingRibbon from './DraftingRibbon.jsx'

afterEach(cleanup)

describe('CockpitTopBand panel focus', () => {
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
