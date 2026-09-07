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
import { deriveWorkspaceProjectState, WORKSPACE_PROJECT_COPY } from '../site/workspaceProjectState.js'

afterEach(cleanup)

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
