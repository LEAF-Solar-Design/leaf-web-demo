import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { surfaceContract } from './productSurfaces.js'
import WorkspaceEnvironment, { ENVIRONMENT_OCCLUDERS } from './WorkspaceEnvironment.jsx'
import { STUDIO_DRAWING_OCCLUDERS } from './drawingOccluders.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'

afterEach(cleanup)
const profiles = ['browser', 'cad', 'solar', 'ios', 'ops', 'lab'].map(id => ({ id, label: id }))
function Host({ scope = 'account/project', content = false, modal = false }) {
  const [profile, setProfile] = useState('browser')
  const [panel, setPanel] = useState('none')
  return <>
    <WorkspaceEnvironment scopeKey={scope} profile={profile} profiles={profiles} onSelect={setProfile}
      panel={panel} onPanel={setPanel} hasContent={content}>
      <label>Project draft<input defaultValue="Initial" /></label>
    </WorkspaceEnvironment>
    <div id="project-workspace-activity" hidden={panel !== 'studio'}>
      <button id="browser-studio-close" onClick={() => setPanel('none')}>Close Studio</button>
      <label>Conversation draft<input defaultValue="Describe a drawing" /></label>
    </div>
    {modal && <Modal />}
  </>
}
function Modal() {
  const [open, setOpen] = useState(true)
  useEscapeOwner('test-modal', open, () => setOpen(false), { layer: 'sheet' })
  return open ? <div role="dialog">Foreground modal</div> : null
}
const environment = container => container.querySelector('.workspace-environment')
const select = profile => fireEvent.change(screen.getByLabelText('Workspace profile'), { target: { value: profile } })

it('enters each profile independently and preserves the project input and entered tabs', () => {
  const { container } = render(<Host />)
  for (const profile of profiles) {
    select(profile.id)
    expect(environment(container).dataset.entered).toBe('false')
    expect(screen.queryByRole('button', { name: 'Continue workspace' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Open project or material' }))
    expect(environment(container).dataset.entered).toBe('true')
    const field = screen.getByLabelText('Project draft')
    if (profile.id === 'browser') fireEvent.change(field, { target: { value: 'Keep draft' } })
    expect(field.value).toBe('Keep draft')
    const chooser = container.querySelector('.environment-chooser')
    expect(chooser.hasAttribute('inert')).toBe(true)
    expect([...chooser.querySelectorAll('button')].every(button => button.tabIndex === -1)).toBe(true)
  }
  for (const profile of profiles) {
    select(profile.id)
    expect(environment(container).dataset.entered).toBe('true')
    expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
  }
  fireEvent.click(screen.getByRole('button', { name: 'Choose' }))
  expect(environment(container).dataset.entered).toBe('false')
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open project or material' }))
})

it('resets every profile on authority change, including a return to the old project', () => {
  const { container, rerender } = render(<Host content />)
  fireEvent.click(screen.getByRole('button', { name: 'Continue workspace' }))
  select('cad')
  fireEvent.click(screen.getByRole('button', { name: 'Continue workspace' }))
  rerender(<Host scope="account/other-project" content />)
  expect(environment(container).dataset.entered).toBe('false')
  select('browser')
  expect(environment(container).dataset.entered).toBe('false')
  rerender(<Host content />)
  expect(environment(container).dataset.entered).toBe('false')
})

it('the existing Start request brings the mounted chooser forward without replacing content', () => {
  const enter = vi.fn()
  const props = { scopeKey: 'scope', profile: 'cad', onPanel: vi.fn(), hasContent: true, onEnter: enter }
  const { container, rerender } = render(<WorkspaceEnvironment {...props}><input defaultValue="Working content" /></WorkspaceEnvironment>)
  const chooser = container.querySelector('.environment-chooser')
  const input = container.querySelector('input')
  fireEvent.click(screen.getByRole('button', { name: 'Continue workspace' }))
  expect(enter).toHaveBeenCalledOnce()
  expect(environment(container).dataset.entered).toBe('true')
  rerender(<WorkspaceEnvironment {...props} chooseRequest={1}><input defaultValue="Working content" /></WorkspaceEnvironment>)
  expect(container.querySelector('.environment-chooser')).toBe(chooser)
  expect(container.querySelector('input')).toBe(input)
  expect(environment(container).dataset.entered).toBe('false')
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open project or material' }))
})

it('uses one edge panel at a time, restores toggle focus, and yields Escape to a foreground modal', () => {
  const { rerender } = render(<Host />)
  const project = screen.getByRole('button', { name: 'Project' })
  fireEvent.click(project)
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close Project' }))
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  expect(project.getAttribute('aria-expanded')).toBe('false')
  rerender(<Host modal />)
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  const studio = screen.getByRole('button', { name: 'Studio' })
  expect(studio.getAttribute('aria-expanded')).toBe('true')
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(studio.getAttribute('aria-expanded')).toBe('false')
  expect(document.activeElement).toBe(studio)
  const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
  act(() => window.dispatchEvent(event))
  expect(event.defaultPrevented).toBe(false)
})

it('wires description to existing controls and has immediate reduced-motion depth changes', () => {
  const describe = vi.fn()
  render(<WorkspaceEnvironment scopeKey="scope" onPanel={vi.fn()} onDescribe={describe} />)
  fireEvent.click(screen.getByRole('button', { name: 'Describe what to create' }))
  expect(describe).toHaveBeenCalledOnce()
  const css = readFileSync(`${process.cwd()}/src/site/workspaceEnvironment.css`, 'utf8')
  expect(css).toMatch(/transition: opacity 210ms ease, transform 210ms ease/)
  expect(css).toMatch(/prefers-reduced-motion: reduce[\s\S]*transition: none !important/)
  expect(css).toContain('scale(.96)')
  expect(css).toContain('calc(100vw - 24px)')
})

it('keeps both panel drafts mounted while toggling and changing profiles', () => {
  const { container } = render(<Host />)
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  const conversation = screen.getByLabelText('Conversation draft')
  fireEvent.change(conversation, { target: { value: 'Keep conversation' } })
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  const project = screen.getByLabelText('Project draft')
  fireEvent.change(project, { target: { value: 'Keep project' } })
  select('cad')
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  expect(screen.getByLabelText('Conversation draft')).toBe(conversation)
  expect(conversation.value).toBe('Keep conversation')
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Studio' }))
  expect(container.querySelector('#project-workspace-activity').hidden).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  expect(screen.getByLabelText('Project draft')).toBe(project)
  expect(project.value).toBe('Keep project')
})

it('reserves drawing space for docked instruments and edge panels, never the whole app', () => {
  for (const entry of STUDIO_DRAWING_OCCLUDERS) expect(ENVIRONMENT_OCCLUDERS).toContain(entry)
  expect(ENVIRONMENT_OCCLUDERS).toContainEqual(['#project-workspace-activity', 'right'])
  expect(ENVIRONMENT_OCCLUDERS).toContainEqual(['.environment-strip', 'top'])
  expect(ENVIRONMENT_OCCLUDERS.some(([selector]) => selector.startsWith('.app'))).toBe(false)
})

it('App keeps the Project owner independent of the edge toggle on every Studio profile', () => {
  const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  let owner
  let panelGate
  const walk = node => {
    if (!node || typeof node.type !== 'string') return
    if (node.type === 'VariableDeclarator' && node.id.name === 'boardHostsProject') owner = node.init
    if (node.type === 'JSXAttribute' && node.name.name === 'panel' && node.value?.expression?.type === 'ConditionalExpression') {
      if (source.slice(node.start, node.end).includes('<CampaignPanel')) panelGate = node.value.expression.test
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value.type === 'string') walk(value)
    }
  }
  walk(tree)
  expect(owner).toBeTruthy()
  expect(panelGate).toBeTruthy()
  const owns = Function('studioGround', 'surfaceSlots', 'boardVisible', 'browserPanel', `return (${source.slice(owner.start, owner.end)})`)
  const hasPanel = Function('boardHostsProject', `return (${source.slice(panelGate.start, panelGate.end)})`)
  for (const profile of ['browser', 'cad', 'solar', 'ios', 'ops', 'lab']) {
    for (const panel of ['none', 'project', 'studio']) {
      const result = owns({}, surfaceContract(profile), false, panel)
      expect(result).toBe(true)
      expect(hasPanel(result)).toBe(true)
    }
  }
  expect(hasPanel(false)).toBe(false)
})
