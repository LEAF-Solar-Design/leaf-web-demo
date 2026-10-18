import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { PersistentSeat } from '../workspace/ProjectWorkspacePanels.jsx'
import LabWidgetsPreview from './LabWidgetsPreview.jsx'
import { labPreviewEnabled, resolveLabPreviewScope } from './labPreview.js'

afterEach(cleanup)

const localEnv = { DEV: true, VITE_LAB_PREVIEW: '1' }
const ready = {
  enabled: true, mock: false, session: { status: 'active', generation: 1 },
  accountScope: 'account:alice', tenant: 'tenant-a', orgId: 'org-a', openProjectId: 'project-a',
  workspace: { project: { project_id: 'project-a', org_id: 'org-a' } }, wsLoading: false,
}
const projectB = {
  openProjectId: 'project-b', workspace: { project: { project_id: 'project-b', org_id: 'org-a' } },
}

// Same lifetime boundary as App: a board destination detaches on profile switches,
// while one unkeyed seat and the accepted editor remain mounted outside that branch.
function Shell({ context = ready, profile = 'browser', env = localEnv }) {
  const [destination, setDestination] = useState(null)
  const enabled = labPreviewEnabled(env)
  return <>
    {profile === 'browser' && <section aria-label="Browser project board">
      {enabled && <div ref={setDestination} />}
    </section>}
    {enabled && <PersistentSeat destination={destination}>
      <LabWidgetsPreview context={{ ...context, enabled }} />
    </PersistentSeat>}
  </>
}

const toggle = () => fireEvent.click(screen.getByRole('button', { name: 'Lab' }))
const add = () => fireEvent.click(screen.getByRole('button', { name: 'Add text' }))
const edit = (text) => fireEvent.change(screen.getByLabelText('Text'), { target: { value: text } })
const preview = () => screen.getByRole('region', { name: 'Preview' })
const selectFirst = () => fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' }))
  .getAllByRole('button')[0])

describe('local Lab build boundary', () => {
  it.each([
    { DEV: false, VITE_LAB_PREVIEW: '1' },
    { DEV: true },
    { DEV: true, VITE_LAB_PREVIEW: '0' },
    { DEV: true, VITE_LAB_PREVIEW: true },
    { DEV: 'true', VITE_LAB_PREVIEW: '1' },
  ])('exposes no affordance or editor for %j', (env) => {
    expect(labPreviewEnabled(env)).toBe(false)
    const { container } = render(<Shell env={env} />)
    expect(screen.queryByRole('button', { name: 'Lab' })).not.toBeInTheDocument()
    expect(container.querySelector('.widgets-view')).toBeNull()
    expect(resolveLabPreviewScope({ ...ready, enabled: labPreviewEnabled(env) }).scopeKey).toBeNull()
  })

  it('requires the development flag even if a caller supplies a ready scope', () => {
    expect(labPreviewEnabled(localEnv)).toBe(true)
    const { container } = render(<LabWidgetsPreview context={{ ...ready, enabled: false }} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('shared-shell Lab editor', () => {
  it('opens a real Widgets sub-tab and edits, previews and removes text', () => {
    render(<Shell />)
    const button = screen.getByRole('button', { name: 'Lab' })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('tab', { name: 'Widgets' })).not.toBeInTheDocument()
    toggle()
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(button.getAttribute('aria-controls'))).toBeVisible()
    const tab = screen.getByRole('tab', { name: 'Widgets' })
    expect(tab).toHaveAttribute('aria-selected', 'true')
    expect(screen.getAllByRole('tab')).toHaveLength(1)
    expect(screen.getByRole('tabpanel', { name: 'Widgets' })).toContainElement(
      screen.getByRole('region', { name: 'Text widget editor' }))
    add()
    edit('A useful widget')
    expect(within(preview()).getByText('A useful widget')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(within(preview()).queryByText('A useful widget')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })

  it('retains the same editor, draft and selection across close and destination detach/reattach', () => {
    const { rerender } = render(<Shell />)
    toggle()
    add()
    edit('First element')
    add()
    edit('Second element')
    selectFirst()
    const editor = screen.getByRole('region', { name: 'Text widget editor' })
    const input = screen.getByLabelText('Text')
    toggle()
    expect(screen.queryByLabelText('Text')).not.toBeVisible()
    toggle()
    expect(screen.getByLabelText('Text')).toBe(input)
    expect(input).toHaveValue('First element')
    for (const profile of ['cad', 'solar-cad', 'ios']) {
      rerender(<Shell profile={profile} />)
      expect(editor.isConnected).toBe(false)
      expect(screen.queryByRole('button', { name: 'Lab' })).not.toBeInTheDocument()
      rerender(<Shell />)
      expect(screen.getByRole('region', { name: 'Text widget editor' })).toBe(editor)
      expect(screen.getByLabelText('Text')).toBe(input)
      expect(input).toHaveValue('First element')
      expect(within(preview()).getByText('Second element')).toBeInTheDocument()
    }
  })

  it('restores independent project drafts through real scope inputs and hides stale hydration', () => {
    const { rerender } = render(<Shell />)
    toggle()
    add()
    edit('Project A first')
    add()
    edit('Project A second')
    selectFirst()
    rerender(<Shell context={{ ...ready, openProjectId: 'project-b', wsLoading: true }} />)
    expect(screen.getByRole('status')).toHaveTextContent('Wait for project loading')
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
    expect(screen.queryByText('Project A first')).not.toBeInTheDocument()
    rerender(<Shell context={{ ...ready, openProjectId: 'project-b' }} />)
    expect(screen.getByRole('status')).toHaveTextContent('selected project')
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    rerender(<Shell context={{ ...ready, ...projectB }} />)
    add()
    edit('Project B only')
    rerender(<Shell />)
    expect(screen.getByLabelText('Text')).toHaveValue('Project A first')
    expect(within(preview()).getByText('Project A second')).toBeInTheDocument()
    expect(screen.queryByText('Project B only')).not.toBeInTheDocument()
    rerender(<Shell context={{ ...ready, ...projectB }} />)
    expect(screen.getByLabelText('Text')).toHaveValue('Project B only')
  })

  it('partitions account and session-generation changes without showing a previous draft', () => {
    const { rerender } = render(<Shell />)
    toggle()
    add()
    edit('Alice session one')
    for (const patch of [
      { accountScope: 'account:bob' },
      { session: { status: 'active', generation: 2 } },
      { tenant: 'tenant-b' },
      { orgId: 'org-b', workspace: { project: { project_id: 'project-a', org_id: 'org-b' } } },
    ]) {
      rerender(<Shell context={{ ...ready, ...patch }} />)
      expect(screen.queryByText('Alice session one')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
      add()
      edit('Separate draft')
      rerender(<Shell />)
      expect(screen.getByLabelText('Text')).toHaveValue('Alice session one')
    }
  })

  it.each([
    [{ mock: true }, 'offline demo'],
    [{ session: { status: 'checking', generation: 1 } }, 'Sign in'],
    [{ session: { status: 'required', generation: 1 }, signedIn: true, token: 'present' }, 'Sign in'],
    [{ accountScope: null }, 'Sign in'],
    [{ accountScope: 'guest' }, 'Sign in'],
    [{ session: { status: 'active' } }, 'session to finish loading'],
    [{ tenant: null }, 'tenant'],
    [{ orgId: null }, 'organization'],
    [{ openProjectId: null }, 'Open a project'],
    [{ wsLoading: true }, 'project loading'],
    [{ workspace: null }, 'project loading'],
    [{ workspace: { project: { project_id: 'other' } } }, 'selected project'],
    [{ workspace: { project: { project_id: 'project-a', org_id: 'other' } } }, 'organization'],
  ])('removes stale content and mutation controls while unresolved: %j', (patch, reason) => {
    const { rerender } = render(<Shell />)
    toggle()
    add()
    edit('Private draft')
    rerender(<Shell context={{ ...ready, ...patch }} />)
    expect(screen.getByRole('status')).toHaveTextContent(reason)
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    expect(screen.queryByText('Private draft')).not.toBeInTheDocument()
    rerender(<Shell />)
    expect(screen.getByLabelText('Text')).toHaveValue('Private draft')
  })
})

describe('public scope boundary', () => {
  it('accepts the existing project shape without requiring extra producer fields', () => {
    expect(resolveLabPreviewScope({ ...ready, workspace: { project: { project_id: 'project-a' } } }))
      .toEqual({ scopeKey: JSON.stringify(['account:alice', 1, 'tenant-a', 'org-a', 'project-a']), reason: null })
  })

  it.each([
    { enabled: false }, { enabled: '1' }, { mock: undefined }, { accountScope: '' }, { accountScope: ' ' },
    { session: { status: 'active', generation: null } },
    { session: { status: 'active', generation: -1 } },
    { session: { status: 'active', generation: NaN } },
    { session: { status: 'active', generation: '1' } },
    { wsLoading: undefined }, { openProjectId: ' ' }, { tenant: '' }, { orgId: '' },
    { workspace: { project: { id: 'project-a' } } },
  ])('withholds a scope for incomplete or ambiguous context %j', (patch) => {
    expect(resolveLabPreviewScope({ ...ready, ...patch }).scopeKey).toBeNull()
  })

  it('encodes scope tuples without separator collisions and ignores profile/panel choice', () => {
    const first = resolveLabPreviewScope({ ...ready, accountScope: 'a/b', tenant: 'c' }).scopeKey
    const second = resolveLabPreviewScope({ ...ready, accountScope: 'a', tenant: 'b/c' }).scopeKey
    expect(first).not.toBe(second)
    expect(resolveLabPreviewScope({ ...ready, profile: 'cad', panel: 'closed' }))
      .toEqual(resolveLabPreviewScope(ready))
  })
})
