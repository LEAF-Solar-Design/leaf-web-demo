/**
 * Card B-U7 acceptance (mechanical coverage sweep):
 *   Every projects-surface component renders all three of loading/empty/error
 *   without crash; enumerated by test over the component registry.
 *
 * A registry entry per projects-surface component (ProjectList, Membership,
 * ExportDialog). Each entry drives that component into its own loading,
 * empty, and error state using the exact same setup its own acceptance test
 * (projectList.test.jsx, membership.test.jsx, export.test.jsx) uses — this
 * file adds no new component behavior, it only sweeps the three states and
 * asserts none of them throws.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
}))

import { listProjects } from './api.js'
import ProjectList from './ProjectList.jsx'
import Membership from './Membership.jsx'
import ExportDialog from './ExportDialog.jsx'

afterEach(cleanup)

const noop = () => {}

const COMPONENT_REGISTRY = [
  {
    name: 'ProjectList',
    async loading() {
      listProjects.mockReturnValue(new Promise(() => {})) // in-flight forever — the loading state itself
      const utils = render(<ProjectList enabled />)
      await waitFor(() => expect(screen.getByRole('status', { name: /loading projects/i })).toBeTruthy())
      return utils
    },
    async empty() {
      listProjects.mockResolvedValue([])
      const utils = render(<ProjectList enabled />)
      await waitFor(() => expect(screen.getByText(/don.t have any projects yet/i)).toBeTruthy())
      return utils
    },
    async error() {
      listProjects.mockRejectedValue(new Error('GET /api/projects -> 500'))
      const utils = render(<ProjectList enabled />)
      await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
      return utils
    },
  },
  {
    name: 'Membership',
    async loading() {
      // No server authority read yet — Membership's own "no guessed matrix"
      // state (membership.test.jsx acceptance #2). No viewerId either: with
      // an empty roster, a truthy viewerId with no matching record would hit
      // the "revoked" branch instead, which is not what "loading" means here.
      const utils = render(
        <Membership authority={null} members={[]} onInvite={noop} onChangeRole={noop} onRevoke={noop} />,
      )
      return utils
    },
    async empty() {
      const utils = render(
        <Membership
          authority={{ role: 'owner', can_invite: true, can_manage: true }}
          members={[]}
          onInvite={vi.fn()}
          onChangeRole={vi.fn()}
          onRevoke={vi.fn()}
        />,
      )
      await waitFor(() => expect(screen.queryAllByRole('listitem').length).toBe(0))
      return utils
    },
    async error() {
      const onInvite = vi.fn().mockRejectedValue(new Error('invite failed'))
      const utils = render(
        <Membership
          authority={{ role: 'owner', can_invite: true, can_manage: true }}
          members={[]}
          onInvite={onInvite}
          onChangeRole={vi.fn()}
          onRevoke={vi.fn()}
        />,
      )
      fireEvent.change(screen.getByLabelText(/invite by email/i), { target: { value: 'a@b.com' } })
      fireEvent.click(screen.getByRole('button', { name: /^invite$/i }))
      await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
      return utils
    },
  },
  {
    name: 'ExportDialog',
    async loading() {
      const onExport = vi.fn(() => new Promise(() => {})) // never settles — the in-flight state itself
      const utils = render(<ExportDialog onExport={onExport} />)
      fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
      await waitFor(() => expect(screen.getByRole('progressbar')).toBeTruthy())
      return utils
    },
    async empty() {
      const utils = render(<ExportDialog onExport={vi.fn()} />)
      expect(screen.queryByText(/^Receipt:/)).toBeNull()
      return utils
    },
    async error() {
      const onExport = vi.fn().mockRejectedValue(new Error('export failed'))
      const utils = render(<ExportDialog onExport={onExport} />)
      fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
      await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
      return utils
    },
  },
]

describe('projects-surface component registry: loading/empty/error coverage sweep', () => {
  it('covers every actual component file in the directory (discovered, not hand-listed)', () => {
    // Real enumeration: glob the directory so a new component added without
    // registry coverage FAILS this test instead of silently going untested.
    const actualFiles = Object.keys(
      import.meta.glob('./*.jsx', { eager: false })
    )
      .map((p) => p.replace('./', '').replace('.jsx', ''))
      .filter((n) => !n.endsWith('.test') && n !== 'states')
    const covered = COMPONENT_REGISTRY.map((c) => c.name).sort()
    expect(actualFiles.sort()).toEqual(covered)
  })

  for (const entry of COMPONENT_REGISTRY) {
    describe(entry.name, () => {
      it('renders a loading state without crashing', async () => {
        const { container } = await entry.loading()
        expect(container).toBeTruthy()
      })

      it('renders an empty state without crashing', async () => {
        const { container } = await entry.empty()
        expect(container).toBeTruthy()
      })

      it('renders an error state without crashing', async () => {
        const { container } = await entry.error()
        expect(container).toBeTruthy()
      })
    })
  }
})
