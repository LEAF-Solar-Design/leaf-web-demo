// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import DemoTour from './DemoTour.jsx'

const steps = [
  { id: 'welcome', title: 'Explore the drawing', body: 'Start here.' },
  { id: 'count', title: 'Count the panels', body: 'Wait for the result.', prompt: 'Count panels' },
  { id: 'done', title: 'Explore freely', body: 'Continue designing.' },
]

function Workspace() {
  const [open, setOpen] = useState(true)
  return <div className="app">
    <textarea data-testid="command-bar" aria-label="Command bar" />
    {open && <DemoTour steps={steps} onExit={() => setOpen(false)} />}
  </div>
}

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
})
afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('guided tour keyboard focus', () => {
  it('moves focus to the new heading before Next becomes disabled', () => {
    render(<DemoTour steps={steps} />)
    const next = screen.getByRole('button', { name: 'Next' })
    next.focus()
    fireEvent.click(next)
    expect(next).toBeDisabled()
    expect(screen.getByRole('heading', { name: 'Count the panels' })).toHaveFocus()
    fireEvent.click(next)
    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument()
  })

  it('returns a direct visitor to the command bar after Skip', () => {
    render(<Workspace />)
    const skip = screen.getByRole('button', { name: 'Skip' })
    skip.focus()
    fireEvent.click(skip)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Command bar' })).toHaveFocus()
  })

  it('returns a direct visitor to the command bar after the banner exit', () => {
    render(<Workspace />)
    const exit = screen.getByRole('button', { name: 'Exit and explore freely' })
    exit.focus()
    fireEvent.click(exit)
    expect(screen.getByRole('textbox', { name: 'Command bar' })).toHaveFocus()
  })

  it('uses the command bar when the saved opener becomes hidden', () => {
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    render(<Workspace />)
    opener.style.display = 'none'
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByRole('textbox', { name: 'Command bar' })).toHaveFocus()
  })

  it('keeps a visible saved opener as the preferred return target', () => {
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(opener).toHaveFocus()
  })

  it('returns to the command bar when the saved opener was removed', () => {
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    render(<Workspace />)
    opener.remove()
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByRole('textbox', { name: 'Command bar' })).toHaveFocus()
  })

  it('uses a visible workspace control when the command bar is hidden', () => {
    const { container } = render(<Workspace />)
    screen.getByRole('textbox', { name: 'Command bar' }).hidden = true
    const entry = document.createElement('button')
    entry.textContent = 'Open drawing'
    container.querySelector('.app').append(entry)
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(entry).toHaveFocus()
  })
})
