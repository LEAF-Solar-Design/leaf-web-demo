/**
 * F-9 (sol-critic finding 1): the header chip reads the SHARED derivation and
 * never derives its own label. The pre-F-9 fallback ended at `projectName`,
 * which is the mounted DRAWING's name -- so that
 * path could still print "Project rooftop_demo" over a drawing, which is the
 * exact bug this change exists to fix.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { act, cleanup, createEvent, fireEvent, render, screen, within } from '@testing-library/react'
import ProjectSwitcher from './ProjectSwitcher.jsx'
import { createWorkspaceController } from '../controllers/workspace/createWorkspaceController.js'
import { deriveWorkspaceProjectState, WORKSPACE_PROJECT_COPY } from '../site/workspaceProjectState.js'
import { readProjectPrincipal, readRecentProjects, writeRecentProjects } from '../lib/recentProjects.js'

afterEach(cleanup)

it('B1-d row5: the mounted App props show creation after an unbound list response', async () => {
  const controller = createWorkspaceController({ authLive: true,
    storage: { getItem: () => null, removeItem: vi.fn() },
    services: { listProjects: vi.fn().mockRejectedValue({ status: 403,
      body: { detail: 'verified subject has no active platform identity binding' } }) },
  })
  await controller.loadProjects()
  const state = controller.getSnapshot()
  render(<ProjectSwitcher orgId={state.orgId} unavailable={state.projectsError} />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByLabelText('Workspace name').value).toBe('My workspace')
  expect(screen.getByRole('button', { name: 'Create workspace org' })).toBeTruthy()
  expect(state.orgId).toBeNull()
  expect(state.projectsError).toBeNull()
  controller.dispose()
})

it('offers workspace creation without bootstrap props or an org', () => {
  render(<ProjectSwitcher />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByLabelText('Workspace name').value).toBe('My workspace')
  expect(screen.getByRole('button', { name: 'Create workspace org' })).toBeTruthy()
  expect(screen.queryByText(/Loading/)).toBeNull()
})

it('shows the legacy unavailable sentence without bootstrap props or an org', () => {
  render(<ProjectSwitcher unavailable="Workspace creation failed." />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByText('Workspace creation failed.')).toBeTruthy()
  expect(screen.queryByText(/Loading/)).toBeNull()
})

it('shows the legacy empty project list without projectsLoaded', () => {
  render(<ProjectSwitcher orgId="o1" />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByText('No projects yet.')).toBeTruthy()
  expect(screen.getByLabelText('New project')).toBeTruthy()
})

it('ignores new draft and recovery props when bootstrapState is omitted', () => {
  render(<ProjectSwitcher orgDraftError="Draft failure." orgConflict />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByLabelText('Workspace name')).toBeTruthy()
  expect(screen.queryByText('Draft failure.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Use my existing workspace' })).toBeNull()
})

it.each(['bound', 'unbound'])('shows equal unavailable and draft errors once when %s', (bootstrapState) => {
  render(<ProjectSwitcher bootstrapState={bootstrapState} unavailable="Creation failed."
    orgDraftError="Creation failed." projectDraftError="Creation failed." />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getAllByText('Creation failed.')).toHaveLength(1)
  expect(screen.getByRole('alert').textContent).toBe('Creation failed.')
  expect(screen.getByLabelText(bootstrapState === 'bound' ? 'New project' : 'Workspace name')).toBeTruthy()
})

it.each(['org', 'project'])('retains a failed legacy %s draft submitted with Enter', async (kind) => {
  const create = vi.fn().mockResolvedValue(null)
  const open = vi.fn()
  render(<ProjectSwitcher orgId={kind === 'project' ? 'o1' : undefined}
    projects={[{ id: 'p1', name: 'Maple' }]} onCreateOrg={create} onCreateProject={create} onOpenProject={open} />)
  fireEvent.click(document.querySelector('.proj-chip'))
  const input = screen.getByLabelText(kind === 'project' ? 'New project' : 'Workspace name')
  fireEvent.change(input, { target: { value: 'Retained draft' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(create).toHaveBeenCalledExactlyOnceWith('Retained draft')
  expect(open).not.toHaveBeenCalled()
  await Promise.resolve()
  expect(input.value).toBe('Retained draft')
})

it('keeps Enter in a create field out of project selection and retains failed drafts', async () => {
  const open = vi.fn()
  const create = vi.fn().mockResolvedValue(null)
  render(<ProjectSwitcher bootstrapState="bound" projects={[{ id: 'p1', name: 'Maple' }]}
    onOpenProject={open} onCreateProject={create} />)
  fireEvent.click(document.querySelector('.proj-chip'))
  const input = screen.getByLabelText('New project')
  fireEvent.change(input, { target: { value: 'Draft project' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(open).not.toHaveBeenCalled()
  expect(create).toHaveBeenCalledWith('Draft project')
  await Promise.resolve()
  expect(input.value).toBe('Draft project')
})

it('uses unbound server state despite an org id and recovers conflicts', () => {
  const retry = vi.fn()
  render(<ProjectSwitcher bootstrapState="unbound" orgId="stale" projects={[]}
    unavailable="verified subject has no active platform identity binding"
    orgConflict orgDraftError="Already bound to a different workspace." onLoadProjects={retry} />)
  fireEvent.click(document.querySelector('.proj-chip'))
  expect(screen.getByLabelText('Workspace name').value).toBe('My workspace')
  expect(screen.getByRole('alert').textContent).toBe('Already bound to a different workspace.')
  fireEvent.click(screen.getByRole('button', { name: 'Use my existing workspace' }))
  expect(retry).toHaveBeenCalledTimes(1)
})

const chip = () => document.querySelector('.proj-chip')

it('shows a Projects affordance and preserves keyboard selection and project creation', () => {
  const onOpenProject = vi.fn()
  const onCreateProject = vi.fn()
  const state = deriveWorkspaceProjectState({ openProjectId: 'p-1', projectName: 'Maple', drawingName: 'drawing', orgId: 'org-1' })
  render(<ProjectSwitcher mock={false} orgId="org-1" projectName="drawing" openProjectId="p-1"
    workspaceProject={state} projects={[{ project_id: 'p-1', name: 'Maple' }, { project_id: 'p-2', name: 'Oak' }]}
    onOpenProject={onOpenProject} onCreateProject={onCreateProject} onCreateOrg={() => {}} />)
  const trigger = screen.getByRole('button', { name: 'Projects: change project. Project Maple' })
  expect(trigger.textContent).toContain('Projects / Change project')
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(trigger)
  expect(trigger.getAttribute('aria-expanded')).toBe('true')
  fireEvent.keyDown(document, { key: 'ArrowDown' })
  fireEvent.keyDown(document, { key: 'Enter' })
  expect(onOpenProject).toHaveBeenCalledExactlyOnceWith('p-2')
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(trigger)
  fireEvent.change(screen.getByLabelText('New project'), { target: { value: ' Birch ' } })
  fireEvent.submit(screen.getByLabelText('New project').closest('form'))
  expect(onCreateProject).toHaveBeenCalledExactlyOnceWith('Birch')
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
})

describe('stu2-h: creation never falls back to a native dialog', () => {
  it('a create request opens the menu on the focused workspace field', () => {
    const onCreateOrg = vi.fn().mockResolvedValue(null)
    const { rerender } = render(<ProjectSwitcher onCreateOrg={onCreateOrg} />)
    expect(chip().getAttribute('aria-expanded')).toBe('false')
    rerender(<ProjectSwitcher createRequest={1} onCreateOrg={onCreateOrg} />)
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    const field = screen.getByLabelText('Workspace name')
    expect(document.activeElement).toBe(field)
    fireEvent.change(field, { target: { value: 'Ridge workspace' } })
    fireEvent.submit(field.closest('form'))
    expect(onCreateOrg).toHaveBeenCalledExactlyOnceWith('Ridge workspace')
  })

  it('each new create request reopens the menu on the focused project field', () => {
    const onCreateProject = vi.fn().mockResolvedValue(null)
    const props = { orgId: 'org-1', projects: [], onCreateProject, onOpenProject: () => {} }
    const { rerender } = render(<ProjectSwitcher {...props} createRequest={1} />)
    expect(document.activeElement).toBe(screen.getByLabelText('New project'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(chip().getAttribute('aria-expanded')).toBe('false')
    rerender(<ProjectSwitcher {...props} createRequest={2} />)
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    const field = screen.getByLabelText('New project')
    expect(document.activeElement).toBe(field)
    fireEvent.change(field, { target: { value: 'Cedar' } })
    fireEvent.submit(field.closest('form'))
    expect(onCreateProject).toHaveBeenCalledExactlyOnceWith('Cedar')
  })

  it('a zero create request leaves the menu closed', () => {
    render(<ProjectSwitcher orgId="org-1" createRequest={0} />)
    expect(chip().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByLabelText('New project')).toBeNull()
  })

  it('App holds no window.prompt for org or project creation', () => {
    const src = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
    expect(src).not.toMatch(/window\.prompt|\bprompt\(/)
    expect(src).toMatch(/createRequest=\{projectCreateRequest\}/)
  })
})

describe('project-service error copy', () => {
  it('shows the permission error and keeps the drawing name', async () => {
    const unavailable = 'platform role does not permit mutation'
    const state = deriveWorkspaceProjectState({
      drawingName: 'rooftop_demo', orgId: 'org-1', projectsUnavailable: unavailable,
    })
    render(<ProjectSwitcher
      mock={false} orgId="org-1" projects={[]} projectName="rooftop_demo"
      unavailable={unavailable} workspaceProject={state}
      onCreateOrg={() => {}} onCreateProject={() => {}} onOpenProject={() => {}}
    />)
    expect(chip().textContent).toContain('Drawing')
    fireEvent.click(chip())
    await screen.findByText(unavailable)
    expect(document.querySelector('.proj-note').textContent).toBe(unavailable)
    expect(document.body.textContent).not.toContain('no database configured')
    expect(document.body.textContent).not.toContain('platform database')
    expect(document.querySelector('.proj-sub').textContent).toContain('rooftop_demo')
  })

  it('shows a neutral service note for boolean unavailability', async () => {
    const state = deriveWorkspaceProjectState({
      drawingName: 'rooftop_demo', orgId: 'org-1', projectsUnavailable: true,
    })
    render(<ProjectSwitcher
      mock={false} orgId="org-1" projects={[]} projectName="rooftop_demo"
      unavailable={true} workspaceProject={state}
      onCreateOrg={() => {}} onCreateProject={() => {}} onOpenProject={() => {}}
    />)
    fireEvent.click(chip())
    await screen.findByText(WORKSPACE_PROJECT_COPY.reasonServiceGeneric)
    expect(document.querySelector('.proj-note').textContent).toBe(WORKSPACE_PROJECT_COPY.reasonServiceGeneric)
  })
})

describe('F-9: the header chip never renames a drawing a project', () => {
  it('a mounted drawing with no workspace project is tagged Drawing', () => {
    const state = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', orgId: 'org-1' })
    render(<ProjectSwitcher mock projectName="rooftop_demo" workspaceProject={state} />)
    expect(chip().textContent).toContain('Drawing')
    expect(chip().textContent).toContain('rooftop_demo')
    expect(chip().textContent).not.toContain('Project')
  })

  it('an open workspace project is tagged Project', () => {
    const state = deriveWorkspaceProjectState({
      openProjectId: 'p-1', projectName: 'Maple St retrofit', drawingName: 'rooftop_demo', orgId: 'org-1',
    })
    render(<ProjectSwitcher mock projectName="rooftop_demo" workspaceProject={state} />)
    expect(chip().textContent).toContain('Project')
    expect(chip().textContent).toContain('Maple St retrofit')
  })

  it('OMITTING the shared state cannot resurrect the drawing-as-project label', () => {
    // The load-bearing case. Before this fix, no workspaceProject meant the
    // chip fell back to the drawing name under a "Project" tag.
    render(<ProjectSwitcher mock projectName="rooftop_demo" />)
    expect(chip().textContent).not.toContain('rooftop_demo')
    expect(chip().textContent).toContain('None open')
  })

  it('the component holds no local label derivation at all', () => {
    const src = readFileSync(`${process.cwd()}/src/components/ProjectSwitcher.jsx`, 'utf8')
    // Usage forms only -- the comments deliberately still NAME the removed
    // fallback so a future reader knows why it must not come back.
    expect(src).not.toContain('currentName ||')
    expect(src).not.toContain('currentName,')
    expect(src).not.toContain('{currentName}')
  })
})

describe('recent and pinned projects', () => {
  const projects = [{ project_id: 'p1', name: 'Maple' }, { id: 'p2', name: 'Oak' }, { id: 'p3', name: 'Birch' }]
  const storageForTest = () => {
    const values = new Map()
    return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
  }
  const names = (group) => within(group).getAllByRole('menuitem').map((row) => row.querySelector('.label').textContent)

  it('shows Pinned and Recent above all projects in the same menu, resolving current names and dropping stale ids', () => {
    const storage = storageForTest()
    writeRecentProjects('alice', { pinned: ['p2', 'removed'], recent: ['p3', 'removed', 'p1'] }, storage)
    render(<ProjectSwitcher orgId="o1" principalId="alice" storage={storage} projects={projects} />)
    fireEvent.click(chip())
    const menu = screen.getByRole('menu')
    const groups = within(menu).getAllByRole('group')
    expect(groups.map((group) => group.getAttribute('aria-label'))).toEqual(['Pinned', 'Recent', 'All projects'])
    expect(names(groups[0])).toEqual(['Oak'])
    expect(names(groups[1])).toEqual(['Birch', 'Maple'])
    expect(names(groups[2])).toEqual(['Maple', 'Oak', 'Birch'])
    expect(within(menu).queryByText('removed')).toBeNull()
  })

  it('pins and unpins without opening a project or closing the menu, persisting across mounts', () => {
    const storage = storageForTest()
    const onOpenProject = vi.fn()
    const props = { orgId: 'o1', principalId: 'alice', storage, projects, onOpenProject }
    const view = render(<ProjectSwitcher {...props} />)
    fireEvent.click(chip())
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Pin Oak' }))
    expect(onOpenProject).not.toHaveBeenCalled()
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    expect(readRecentProjects('alice', storage)).toEqual({ recent: [], pinned: ['p2'] })
    view.unmount()
    render(<ProjectSwitcher {...props} />)
    fireEvent.click(chip())
    const pinned = screen.getByRole('group', { name: 'Pinned' })
    const unpin = within(pinned).getByRole('menuitemcheckbox', { name: 'Unpin Oak' })
    expect(unpin.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(unpin)
    expect(screen.queryByRole('group', { name: 'Pinned' })).toBeNull()
    expect(readRecentProjects('alice', storage).pinned).toEqual([])
    expect(onOpenProject).not.toHaveBeenCalled()
  })

  it('remembers mouse and keyboard opens and navigates through the displayed group order', () => {
    const storage = storageForTest()
    const onOpenProject = vi.fn()
    render(<ProjectSwitcher orgId="o1" principalId="alice" storage={storage} projects={projects} onOpenProject={onOpenProject} />)
    fireEvent.click(chip())
    fireEvent.click(screen.getByRole('menuitem', { name: 'Oak' }))
    expect(onOpenProject).toHaveBeenLastCalledWith('p2')
    expect(readRecentProjects('alice', storage).recent).toEqual(['p2'])
    expect(chip().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(chip())
    expect(names(screen.getByRole('group', { name: 'Recent' }))).toEqual(['Oak'])
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onOpenProject).toHaveBeenLastCalledWith('p1')
    expect(onOpenProject).toHaveBeenCalledTimes(2)
    expect(readRecentProjects('alice', storage).recent).toEqual(['p1', 'p2'])
    expect(chip().getAttribute('aria-expanded')).toBe('false')
  })

  it('records externally opened projects and isolates a principal switch even within the same org', () => {
    const storage = storageForTest()
    writeRecentProjects('alice', { pinned: ['p1'], recent: [] }, storage)
    const props = { orgId: 'shared-org', storage, projects }
    const { rerender } = render(<ProjectSwitcher {...props} principalId="alice" openProjectId="p1" />)
    rerender(<ProjectSwitcher {...props} principalId="alice" openProjectId="p2" />)
    expect(readRecentProjects('alice', storage).recent).toEqual(['p2', 'p1'])
    fireEvent.click(chip())
    expect(names(screen.getByRole('group', { name: 'Pinned' }))).toEqual(['Maple'])
    rerender(<ProjectSwitcher {...props} principalId="bob" />)
    expect(screen.queryByRole('group', { name: 'Pinned' })).toBeNull()
    expect(screen.queryByRole('group', { name: 'Recent' })).toBeNull()
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Pin Birch' }))
    expect(readRecentProjects('bob', storage)).toEqual({ recent: [], pinned: ['p3'] })
    expect(readRecentProjects('alice', storage).pinned).toEqual(['p1'])
    rerender(<ProjectSwitcher {...props} principalId="alice" />)
    expect(names(screen.getByRole('group', { name: 'Pinned' }))).toEqual(['Maple'])
    expect(names(screen.getByRole('group', { name: 'Recent' }))).toEqual(['Oak', 'Maple'])
  })

  it('uses the mounted caller bearer without requiring a principal prop, and avoids anonymous persistence', () => {
    const storage = storageForTest()
    storage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ iss: 'issuer', sub: 'alice' }))}.signature`)
    const principal = readProjectPrincipal(storage)
    writeRecentProjects(principal, { pinned: ['p2'], recent: [] }, storage)
    const { rerender } = render(<ProjectSwitcher orgId="o1" storage={storage} projects={projects} />)
    fireEvent.click(chip())
    expect(names(screen.getByRole('group', { name: 'Pinned' }))).toEqual(['Oak'])
    storage.setItem('leaf.jwt', '')
    rerender(<ProjectSwitcher orgId="o1" storage={storage} projects={projects} />)
    expect(screen.queryByRole('group', { name: 'Pinned' })).toBeNull()
    expect(screen.queryByRole('menuitemcheckbox')).toBeNull()
    expect(names(screen.getByRole('group', { name: 'All projects' }))).toEqual(['Maple', 'Oak', 'Birch'])
  })

  it('keeps pinning and opening usable when storage throws, retaining this mount preferences', () => {
    const storage = { getItem: () => { throw new Error('locked') }, setItem: () => { throw new Error('full') } }
    const onOpenProject = vi.fn()
    render(<ProjectSwitcher orgId="o1" principalId="alice" storage={storage} projects={projects} onOpenProject={onOpenProject} />)
    fireEvent.click(chip())
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Pin Oak' }))
    const pinned = screen.getByRole('group', { name: 'Pinned' })
    expect(names(pinned)).toEqual(['Oak'])
    fireEvent.click(within(pinned).getByRole('menuitem', { name: /^Oak\b/ }))
    expect(onOpenProject).toHaveBeenCalledExactlyOnceWith('p2')
    fireEvent.click(chip())
    expect(names(screen.getByRole('group', { name: 'Recent' }))).toEqual(['Oak'])
    expect(names(screen.getByRole('group', { name: 'Pinned' }))).toEqual(['Oak'])
  })

  it('keeps the mock chip static even with saved preferences and an open project', () => {
    const storage = storageForTest()
    writeRecentProjects('alice', { pinned: ['p2'], recent: [] }, storage)
    render(<ProjectSwitcher mock principalId="alice" storage={storage} projects={projects} openProjectId="p1" />)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(readRecentProjects('alice', storage)).toEqual({ recent: [], pinned: ['p2'] })
  })
})


function retainedKey(target, key, marks, modifiers = {}) {
  const event = createEvent.keyDown(target, {
    key, bubbles: true, cancelable: true, ...modifiers, ...marks,
  })
  expect(event.isComposing).toBe(marks.isComposing)
  expect(event.keyCode).toBe(marks.keyCode)
  const stop = event.stopPropagation.bind(event)
  event.stopPropagation = vi.fn(() => stop())
  return event
}

function expectYielded(event) {
  expect(event.defaultPrevented).toBe(false)
  expect(event.stopPropagation).not.toHaveBeenCalled()
}

function compositionProjectMenu(bootstrapState) {
  const onCreateOrg = vi.fn().mockResolvedValue(null)
  const onCreateProject = vi.fn().mockResolvedValue(null)
  const onOpenProject = vi.fn()
  const storage = { getItem: () => null, setItem: vi.fn() }
  const view = render(<ProjectSwitcher bootstrapState={bootstrapState} orgId={bootstrapState === 'bound' ? 'o1' : undefined}
    projects={[{ id: 'p1', name: 'Maple' }, { id: 'p2', name: 'Oak' }]} storage={storage} principalId={null}
    onCreateOrg={onCreateOrg} onCreateProject={onCreateProject} onOpenProject={onOpenProject} />)
  fireEvent.click(chip())
  return { ...view, onCreateOrg, onCreateProject, onOpenProject }
}

async function projectCreationComposition(marks) {
  for (const bootstrapState of ['unbound', 'bound']) {
    const view = compositionProjectMenu(bootstrapState)
    try {
      const input = screen.getByLabelText(bootstrapState === 'unbound' ? 'Workspace name' : 'New project')
      if (bootstrapState === 'bound') fireEvent.mouseEnter(view.container.querySelectorAll('.resolver-row')[1])
      fireEvent.change(input, { target: { value: 'Retained draft' } })
      input.focus()
      const highlighted = view.container.querySelector('.resolver-row.active')
      const event = retainedKey(input, 'Enter', marks)
      fireEvent(input, event)
      expectYielded(event)
      expect(view.onCreateOrg).not.toHaveBeenCalled()
      expect(view.onCreateProject).not.toHaveBeenCalled()
      expect(view.onOpenProject).not.toHaveBeenCalled()
      expect(input.value).toBe('Retained draft')
      expect(input).toHaveFocus()
      expect(view.container.querySelector('.resolver-row.active')).toBe(highlighted)
      expect(chip()).toHaveAttribute('aria-expanded', 'true')
      const ordinary = retainedKey(input, 'Enter', { isComposing: false, keyCode: 0 })
      await act(async () => { fireEvent(input, ordinary) })
      expect(ordinary.defaultPrevented).toBe(true)
      const create = bootstrapState === 'unbound' ? view.onCreateOrg : view.onCreateProject
      const other = bootstrapState === 'unbound' ? view.onCreateProject : view.onCreateOrg
      expect(create).toHaveBeenCalledExactlyOnceWith('Retained draft')
      expect(other).not.toHaveBeenCalled()
      expect(view.onOpenProject).not.toHaveBeenCalled()
      expect(input.value).toBe('Retained draft')
      expect(view.container.querySelector('.resolver-row.active')).toBe(highlighted)
      expect(chip()).toHaveAttribute('aria-expanded', 'true')
    } finally {
      view.unmount()
    }
  }
}

it('KEYS-D27 project menu creation yields native composition', async () => {
  await projectCreationComposition({ isComposing: true, keyCode: 0 })
})

it('KEYS-D28 project menu creation yields key code 229', async () => {
  await projectCreationComposition({ isComposing: false, keyCode: 229 })
})

it('KEYS-D29 project menu arrows remain with creation inputs', () => {
  for (const bootstrapState of ['unbound', 'bound']) {
    const view = compositionProjectMenu(bootstrapState)
    try {
      const input = screen.getByLabelText(bootstrapState === 'unbound' ? 'Workspace name' : 'New project')
      if (bootstrapState === 'bound') fireEvent.mouseEnter(view.container.querySelectorAll('.resolver-row')[1])
      fireEvent.change(input, { target: { value: 'Retained draft' } })
      input.focus()
      const highlighted = view.container.querySelector('.resolver-row.active')
      for (const key of ['ArrowDown', 'ArrowUp']) {
        for (const marks of [{ isComposing: true, keyCode: 0 }, { isComposing: false, keyCode: 229 }]) {
          const event = retainedKey(input, key, marks)
          fireEvent(input, event)
          expectYielded(event)
          expect(input.value).toBe('Retained draft')
          expect(input).toHaveFocus()
          expect(view.container.querySelector('.resolver-row.active')).toBe(highlighted)
          expect(view.onCreateOrg).not.toHaveBeenCalled()
          expect(view.onCreateProject).not.toHaveBeenCalled()
          expect(view.onOpenProject).not.toHaveBeenCalled()
          const ordinary = retainedKey(input, key, { isComposing: false, keyCode: 0 })
          fireEvent(input, ordinary)
          expectYielded(ordinary)
          expect(input.value).toBe('Retained draft')
          expect(input).toHaveFocus()
          expect(view.container.querySelector('.resolver-row.active')).toBe(highlighted)
          expect(chip()).toHaveAttribute('aria-expanded', 'true')
          expect(view.onCreateOrg).not.toHaveBeenCalled()
          expect(view.onCreateProject).not.toHaveBeenCalled()
          expect(view.onOpenProject).not.toHaveBeenCalled()
        }
      }
    } finally {
      view.unmount()
    }
  }
})

it('KEYS-D30 project menu navigation yields marked native events', () => {
  for (const marks of [{ isComposing: true, keyCode: 0 }, { isComposing: false, keyCode: 229 }]) {
    for (const key of ['ArrowDown', 'ArrowUp', 'Enter']) {
      const view = compositionProjectMenu('bound')
      try {
        const rows = view.container.querySelectorAll('.resolver-row')
        const seed = key === 'ArrowDown' ? 0 : 1
        fireEvent.mouseEnter(rows[seed])
        chip().focus()
        const origin = document.activeElement
        expect(rows[seed]).toHaveClass('active')
        const event = retainedKey(document, key, marks)
        fireEvent(document, event)
        expectYielded(event)
        expect(view.container.querySelector('.resolver-row.active')).toBe(rows[seed])
        expect(chip()).toHaveAttribute('aria-expanded', 'true')
        expect(document.activeElement).toBe(origin)
        expect(view.onOpenProject).not.toHaveBeenCalled()
        expect(view.onCreateOrg).not.toHaveBeenCalled()
        expect(view.onCreateProject).not.toHaveBeenCalled()
        const ordinary = retainedKey(document, key, { isComposing: false, keyCode: 0 })
        fireEvent(document, ordinary)
        expect(ordinary.defaultPrevented).toBe(true)
        if (key === 'Enter') {
          expect(view.onOpenProject).toHaveBeenCalledExactlyOnceWith('p2')
          expect(chip()).toHaveAttribute('aria-expanded', 'false')
        } else {
          expect(view.container.querySelector('.resolver-row.active')).toBe(rows[1 - seed])
          expect(view.onOpenProject).not.toHaveBeenCalled()
          expect(chip()).toHaveAttribute('aria-expanded', 'true')
        }
      } finally {
        view.unmount()
      }
    }
  }
})
