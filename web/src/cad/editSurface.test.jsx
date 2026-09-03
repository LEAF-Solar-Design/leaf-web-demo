/**
 * EditSurface. Card C1-6 acceptance oracle, one describe block per assertion:
 *   - Dormant editing surface mounts only with cad_edit on; renders the
 *     ribbon/command skeleton (OpenCADStudio interaction model, the Leaf Automation
 *     implementation); every command stub reports 'not yet enabled' truthfully.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import EditSurface from './EditSurface.jsx'

afterEach(cleanup)

describe('acceptance: dormant editing surface mounts only with cad_edit on', () => {
  it('renders nothing when the flag is off', () => {
    const { container } = render(<EditSurface enabled={false} />)
    expect(container.firstChild).toBeNull()
  })

  it('does not mount by default (env flag unset in this test build)', () => {
    const { container } = render(<EditSurface />)
    expect(container.firstChild).toBeNull()
  })

  it('mounts the surface when the flag is explicitly on', () => {
    const { container } = render(<EditSurface enabled />)
    expect(container.firstChild).not.toBeNull()
    expect(screen.getByLabelText('CAD editing surface')).toBeInTheDocument()
  })
})

describe("acceptance: renders the ribbon/command skeleton (OpenCADStudio interaction model, the Leaf Automation implementation)", () => {
  it('renders a ribbon tablist with multiple tabs, one selected', () => {
    render(<EditSurface enabled />)
    const tablist = screen.getByRole('tablist', { name: /editing ribbon tabs/i })
    const tabs = within(tablist).getAllByRole('tab')
    expect(tabs.length).toBeGreaterThan(1)
    expect(tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('renders a command toolbar of grouped commands for the active tab', () => {
    render(<EditSurface enabled />)
    const toolbar = screen.getByRole('toolbar')
    const groups = within(toolbar).getAllByRole('group')
    expect(groups.length).toBeGreaterThan(0)
    const commandButtons = within(toolbar).getAllByRole('button')
    expect(commandButtons.length).toBeGreaterThan(1)
  })

  it('switching ribbon tabs swaps the visible command toolbar', () => {
    render(<EditSurface enabled />)
    expect(screen.getByRole('toolbar', { name: /Sketch commands/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Modify' }))

    expect(screen.getByRole('toolbar', { name: /Modify commands/i })).toBeInTheDocument()
    expect(screen.queryByRole('toolbar', { name: /Sketch commands/i })).toBeNull()
  })
})

describe("acceptance: every command stub reports 'not yet enabled' truthfully", () => {
  it('every command button is truthfully labeled not-yet-enabled before any interaction', () => {
    render(<EditSurface enabled />)
    const toolbar = screen.getByRole('toolbar')
    const commandButtons = within(toolbar).getAllByRole('button')
    expect(commandButtons.length).toBeGreaterThan(0)
    for (const button of commandButtons) {
      expect(button.title).toMatch(/not yet enabled/i)
    }
  })

  it('invoking a command stub reports "not yet enabled" via the live status region, never a silent success', () => {
    render(<EditSurface enabled />)
    expect(screen.getByRole('status')).toHaveTextContent('')

    fireEvent.click(screen.getByRole('button', { name: 'Line' }))

    expect(screen.getByRole('status')).toHaveTextContent('"Line" is not yet enabled.')
  })

  it('every command across every ribbon tab reports not-yet-enabled when invoked', () => {
    render(<EditSurface enabled />)
    const tabs = screen.getAllByRole('tab')

    for (const tab of tabs) {
      fireEvent.click(tab)
      const toolbar = screen.getByRole('toolbar')
      const commandButtons = within(toolbar).getAllByRole('button')
      for (const button of commandButtons) {
        const name = button.textContent
        fireEvent.click(button)
        expect(screen.getByRole('status')).toHaveTextContent(`"${name}" is not yet enabled.`)
      }
    }
  })
})
