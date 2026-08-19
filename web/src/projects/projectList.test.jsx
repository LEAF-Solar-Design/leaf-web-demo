/**
 * ProjectList. Card B-U1 acceptance oracle, one describe block per assertion:
 *   1. Signed-in user sees their projects (server-derived membership only);
 *      create names a project and lands in it; empty state is truthful.
 *   2. Loading/error/empty states all rendered; no state renders a blank screen.
 *   3. With lifecycle_ui off the surface is absent (fence assertion in test).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
}))

import { listProjects, createProject } from './api.js'
import ProjectList from './ProjectList.jsx'

afterEach(cleanup)

const PROJECTS = [
  { project_id: 'p-1', name: 'Rooftop A', status: 'active' },
  { project_id: 'p-2', name: 'Rooftop B', status: 'active' },
]

describe('acceptance #1: signed-in user sees their projects (server-derived only); create names + lands in a project; empty state is truthful', () => {
  it('renders exactly the projects the server returned — no client-side filtering or re-derivation', async () => {
    listProjects.mockResolvedValue(PROJECTS)
    render(<ProjectList enabled />)
    await waitFor(() => expect(screen.getByText('Rooftop A')).toBeTruthy())
    expect(screen.getByText('Rooftop B')).toBeTruthy()
  })

  it('creating a project names it, sends that exact name, and lands the user in it', async () => {
    listProjects.mockResolvedValue([])
    const created = { project_id: 'p-new', name: 'New Roof', status: 'active' }
    createProject.mockResolvedValue(created)
    const onOpenProject = vi.fn()
    render(<ProjectList enabled onOpenProject={onOpenProject} />)
    await waitFor(() => expect(screen.getByText(/don.t have any projects yet/i)).toBeTruthy())

    fireEvent.change(screen.getByLabelText(/new project name/i), { target: { value: 'New Roof' } })
    fireEvent.click(screen.getByRole('button', { name: /create project/i }))

    await waitFor(() => expect(createProject).toHaveBeenCalledWith('New Roof'))
    await waitFor(() => expect(onOpenProject).toHaveBeenCalledWith(created))
    expect(screen.getByText(/now in/i).textContent).toMatch(/New Roof/)
    // The name field clears once the create lands — no stale draft left behind.
    expect(screen.getByLabelText(/new project name/i).value).toBe('')
  })

  it('an empty project list renders a truthful empty state, never a guessed project', async () => {
    listProjects.mockResolvedValue([])
    render(<ProjectList enabled />)
    await waitFor(() => expect(screen.getByText(/don.t have any projects yet/i)).toBeTruthy())
    expect(screen.queryByRole('listitem')).toBeNull()
  })
})

describe('acceptance #2: loading/error/empty states all render; no state renders a blank screen', () => {
  it('renders a loading state while the initial fetch is in flight', () => {
    let resolveFetch
    listProjects.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve }))
    const { container } = render(<ProjectList enabled />)
    expect(container.firstChild).not.toBeNull()
    expect(screen.getByRole('status', { name: /loading projects/i })).toBeTruthy()
    resolveFetch([])
  })

  it('renders an error state with a retry affordance when the fetch fails, never a blank screen', async () => {
    listProjects.mockRejectedValue(new Error('GET /api/projects -> 500'))
    const { container } = render(<ProjectList enabled />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(container.firstChild).not.toBeNull()
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()

    listProjects.mockResolvedValue(PROJECTS)
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(screen.getByText('Rooftop A')).toBeTruthy())
  })

  it('renders a truthful empty state, never a blank screen', async () => {
    listProjects.mockResolvedValue([])
    const { container } = render(<ProjectList enabled />)
    await waitFor(() => expect(screen.getByText(/don.t have any projects yet/i)).toBeTruthy())
    expect(container.firstChild).not.toBeNull()
  })
})

describe('acceptance #3: with lifecycle_ui off the surface is absent (fence)', () => {
  it('renders nothing and makes no request when the flag is off', () => {
    const { container } = render(<ProjectList enabled={false} />)
    expect(container.firstChild).toBeNull()
    expect(listProjects).not.toHaveBeenCalled()
  })
})
