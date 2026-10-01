/**
 * F-9 (sol-critic finding 1): the header chip reads the SHARED derivation and
 * never derives its own label. The pre-F-9 fallback ended at `projectName`,
 * which is the mounted DRAWING's name -- so that
 * path could still print "Project rooftop_demo" over a drawing, which is the
 * exact bug this change exists to fix.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ProjectSwitcher from './ProjectSwitcher.jsx'
import { createWorkspaceController } from '../controllers/workspace/createWorkspaceController.js'
import { deriveWorkspaceProjectState, WORKSPACE_PROJECT_COPY } from '../site/workspaceProjectState.js'

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
