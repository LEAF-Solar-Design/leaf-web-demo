import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import ProjectWorkspaceControls, { useProjectWorkspaceLayout } from './ProjectWorkspaceControls.jsx'

afterEach(cleanup)

function Draft() {
  const [value, setValue] = useState('')
  return <input aria-label="Tool draft" value={value} onChange={event => setValue(event.target.value)} />
}

function Workspace({ mock = false, projectId = 'one', surface = 'browser' }) {
  const layout = useProjectWorkspaceLayout({ mock, projectId, surface })
  return (
    <div className="app" ref={layout.appRef}>
      <ProjectWorkspaceControls {...layout} />
      <button onClick={layout.revealTools}>Author a tool</button>
      <aside className="nav" style={{ display: layout.active && !layout.toolsOpen ? 'none' : undefined }}><Draft /></aside>
      <div className="rail-stack" style={{ display: layout.active && !layout.activityOpen ? 'none' : undefined }}><button>Activity entry</button></div>
    </div>
  )
}

describe('project workspace panels', () => {
  it('starts collapsed and toggles each existing panel independently through named controls', () => {
    render(<Workspace />)
    const tools = screen.getByRole('button', { name: 'Tools' })
    const activity = screen.getByRole('button', { name: 'Activity' })
    for (const button of [tools, activity]) {
      expect(button.getAttribute('aria-expanded')).toBe('false')
      expect(document.getElementById(button.getAttribute('aria-controls'))).toBeTruthy()
    }
    fireEvent.click(tools)
    expect(tools.getAttribute('aria-expanded')).toBe('true')
    expect(activity.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(activity)
    expect(screen.getByRole('button', { name: 'Activity entry' })).toBeTruthy()
    fireEvent.click(tools)
    expect(tools.getAttribute('aria-expanded')).toBe('false')
    expect(activity.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(activity)
    expect(screen.queryByRole('button', { name: 'Activity entry' })).toBeNull()
  })

  it('keeps a stateful tool child and its DOM node across closing and reopening', () => {
    render(<Workspace />)
    const tools = screen.getByRole('button', { name: 'Tools' })
    fireEvent.click(tools)
    const draft = screen.getByRole('textbox', { name: 'Tool draft' })
    fireEvent.change(draft, { target: { value: 'Keep this draft' } })
    draft.focus()
    fireEvent.click(tools)
    expect(document.activeElement).toBe(tools)
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(tools)
    expect(screen.getByRole('textbox')).toBe(draft)
    expect(draft.value).toBe('Keep this draft')
  })

  it('resets selection on project and profile changes without clearing mounted child state', () => {
    const { rerender } = render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Tools' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Retained' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activity' }))
    rerender(<Workspace projectId="two" />)
    expect(screen.getByRole('button', { name: 'Tools' }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByRole('button', { name: 'Activity' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Tools' }))
    expect(screen.getByRole('textbox').value).toBe('Retained')
    rerender(<Workspace projectId="two" surface="cad" />)
    expect(screen.queryByRole('group', { name: 'Project workspace panels' })).toBeNull()
    expect(screen.getByRole('textbox').value).toBe('Retained')
    rerender(<Workspace projectId="two" />)
    expect(screen.getByRole('button', { name: 'Tools' }).getAttribute('aria-expanded')).toBe('false')
  })

  it.each([{ mock: true }, { projectId: null }, { surface: 'cad' }, { surface: 'solar' }, { surface: 'ios' }])('leaves existing panels available in inactive mode %j', props => {
    render(<Workspace {...props} />)
    expect(screen.queryByRole('group', { name: 'Project workspace panels' })).toBeNull()
    expect(screen.getByRole('textbox')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Activity entry' })).toBeTruthy()
  })

  it('reveals Tools on every authoring request even after a manual collapse', () => {
    render(<Workspace />)
    const author = screen.getByRole('button', { name: 'Author a tool' })
    const tools = screen.getByRole('button', { name: 'Tools' })
    fireEvent.click(author)
    expect(tools.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(tools)
    fireEvent.click(author)
    expect(tools.getAttribute('aria-expanded')).toBe('true')
  })
})
