import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ProjectStartPanel from './ProjectStartPanel.jsx'

afterEach(cleanup)

it('shows a quiet loading state before workspace bootstrap resolves', () => {
  render(<ProjectStartPanel bootstrapState="unknown" />)
  expect(screen.getByRole('status').textContent).toBe('Loading workspace projects…')
  expect(screen.queryByText('No projects yet.')).toBeNull()
})

it('offers the prefilled workspace form and retains a failed draft', async () => {
  const onCreateOrg = vi.fn().mockResolvedValue(null)
  render(<ProjectStartPanel bootstrapState="unbound" onCreateOrg={onCreateOrg} orgDraftError="Workspace creation failed." />)
  const input = screen.getByLabelText('Workspace name')
  expect(input.value).toBe('My workspace')
  fireEvent.change(input, { target: { value: 'Draft workspace' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => expect(onCreateOrg).toHaveBeenCalledWith('Draft workspace'))
  expect(input.value).toBe('Draft workspace')
  expect(screen.getByRole('alert').textContent).toBe('Workspace creation failed.')
})

it('shows empty copy only after a successful empty list', () => {
  const { rerender } = render(<ProjectStartPanel bootstrapState="bound" projectsLoading />)
  expect(screen.getByRole('status').textContent).toBe('Loading projects…')
  expect(screen.queryByText('No projects yet.')).toBeNull()
  rerender(<ProjectStartPanel bootstrapState="bound" />)
  expect(screen.queryByText('No projects yet.')).toBeNull()
  rerender(<ProjectStartPanel bootstrapState="bound" projectsLoaded />)
  expect(screen.getByText('No projects yet.')).toBeTruthy()
})

it('offers an explained retry when unavailable', () => {
  const retry = vi.fn()
  render(<ProjectStartPanel bootstrapState="unavailable" projectsError="The project service is offline." onLoadProjects={retry} />)
  expect(screen.getByRole('alert').textContent).toBe('The project service is offline.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(retry).toHaveBeenCalledTimes(1)
})

it('recovers a conflict without creating another workspace', () => {
  const retry = vi.fn()
  const create = vi.fn()
  render(<ProjectStartPanel bootstrapState="unbound" orgConflict orgDraftError="Already bound to another workspace." onLoadProjects={retry} onCreateOrg={create} />)
  fireEvent.click(screen.getByRole('button', { name: 'Use my existing workspace' }))
  expect(retry).toHaveBeenCalledTimes(1)
  expect(create).not.toHaveBeenCalled()
})

it('does not promise drawing attachment without a handler', () => {
  render(<ProjectStartPanel bootstrapState="bound" drawingMounted />)
  expect(screen.getByRole('button', { name: 'Create project' })).toBeTruthy()
  expect(screen.queryByText('Create project from this drawing')).toBeNull()
})

it('creates before attaching the drawing to the returned project', async () => {
  const create = vi.fn().mockResolvedValue({ project_id: 'p1' })
  const attach = vi.fn()
  render(<ProjectStartPanel bootstrapState="bound" drawingMounted onCreateProject={create} onAttachDrawing={attach} />)
  fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Maple' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create project from this drawing' }))
  await waitFor(() => expect(attach).toHaveBeenCalledWith('p1'))
  expect(create).toHaveBeenCalledWith('Maple')
})

it('navigates the project picker with arrow keys', () => {
  const open = vi.fn()
  render(<ProjectStartPanel bootstrapState="bound" projects={[{ id: 'a', name: 'Maple' }, { id: 'b', name: 'Oak' }]} onOpenProject={open} />)
  fireEvent.keyDown(screen.getByRole('button', { name: 'Maple' }), { key: 'ArrowDown' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Oak' }))
  fireEvent.click(document.activeElement)
  expect(open).toHaveBeenCalledWith('b')
})
