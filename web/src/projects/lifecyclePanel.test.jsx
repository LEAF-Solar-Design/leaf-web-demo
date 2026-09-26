/**
 * ProjectLifecyclePanel mount oracle, plus the ToolCast wiring that decides
 * when it mounts at all.
 *
 * Two things have to be true for the graft to be correct, and neither one
 * proves the other:
 *   1. The panel renders the lifecycle block for an open project when the flag
 *      is on, and renders NOTHING when it is off (this file, DOM assertions).
 *   2. ToolCast reaches it through the exact gate — the build flag first, then
 *      the public-demo / mock-transport / operator fences, then an open project
 *      id (this file, source assertions against ToolCast.jsx).
 *
 * ToolCast itself is not mounted here on purpose: it pulls three.js, the wasm
 * CAD harness, seven controllers and the whole mock engine, so a jsdom mount of
 * it would test the mocks, not the wiring. The wiring is a static fact about
 * the file, so it is asserted as one — the same technique
 * web/src/app-wiring.test.mjs already uses for App.jsx's bindings.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

vi.mock('./api.js', () => ({
  createBlankProject: vi.fn(),
  cloneProject: vi.fn(),
  deleteProject: vi.fn(),
  exportProject: vi.fn(),
  getOrgIdentities: vi.fn(),
  getProjectLifecycle: vi.fn(),
  inviteMember: vi.fn(),
  resetProject: vi.fn(),
  revokeMember: vi.fn(),
  setIdentityDisplayName: vi.fn(),
}))

vi.mock('./flag.js', () => ({ ENV_LIFECYCLE_UI: true }))

import { cloneProject, deleteProject, exportProject, getOrgIdentities, getProjectLifecycle, inviteMember, resetProject, setIdentityDisplayName } from './api.js'
import { renderHook } from '@testing-library/react'
import useProjectLifecycle from './useProjectLifecycle.js'
import ProjectLifecyclePanel from './ProjectLifecyclePanel.jsx'

afterEach(cleanup)

const PROJECT_ID = '11111111-2222-4333-8444-555555555555'
const VIEWER_BINDING = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

const SNAPSHOT = {
  project: { project_id: PROJECT_ID, name: 'Rooftop Array', status: 'active', profile: 'blank_browser' },
  // Server-supplied viewer identity. The browser has no other way to know which
  // roster row is "you": no route echoes an actor binding id back to a client.
  viewer: {
    binding_id: VIEWER_BINDING,
    membership_id: 'm-1',
    role: 'owner',
    can_invite: true,
    can_manage: true,
  },
  members: [
    { membership_id: 'm-1', binding_id: VIEWER_BINDING, role: 'owner', status: 'active', created_at: '2026-08-01T00:00:00+00:00', revoked_at: null },
    { membership_id: 'm-2', binding_id: 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff', role: 'read_only', status: 'active', created_at: '2026-08-02T00:00:00+00:00', revoked_at: null },
  ],
  files: [],
  receipts: [
    { receipt_id: 'r-1', project_id: PROJECT_ID, action: 'project_created', input_digest: 'd'.repeat(64), created_at: '2026-08-01T00:00:00+00:00' },
  ],
}

it('B5 an org owner names a member from the lifecycle panel', async () => {
  const orgId = 'cccccccc-dddd-4eee-8fff-111111111111'
  const memberBindingId = SNAPSHOT.members[1].binding_id
  const initial = {
    ...SNAPSHOT,
    project: { ...SNAPSHOT.project, org_id: orgId },
    viewer: { ...SNAPSHOT.viewer, can_label_identities: true },
  }
  getProjectLifecycle.mockResolvedValue(initial)
  setIdentityDisplayName.mockImplementation(async () => {
    getProjectLifecycle.mockResolvedValue({
      ...initial,
      members: initial.members.map((member) => member.binding_id === memberBindingId
        ? { ...member, label: 'Ada Lovelace' } : member),
    })
    return { identity: { binding_id: memberBindingId, label: 'Ada Lovelace' } }
  })
  render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} projectName="Rooftop Array" />)
  const input = await screen.findByLabelText('Display name for Member bbbbbbbb')
  fireEvent.change(input, { target: { value: '  Ada Lovelace  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Member bbbbbbbb' }))
  await waitFor(() => expect(setIdentityDisplayName).toHaveBeenCalledWith(orgId, memberBindingId, 'Ada Lovelace'))
  await waitFor(() => expect(screen.getByText('Ada Lovelace · joined 2026-08-02')).toBeTruthy())
})

it('B5 a rename updates the loaded picker without another request', async () => {
  const orgId = 'cccccccc-dddd-4eee-8fff-111111111111'
  const initial = {
    ...SNAPSHOT,
    project: { ...SNAPSHOT.project, org_id: orgId },
    viewer: { ...SNAPSHOT.viewer, can_label_identities: true },
  }
  const identity = { binding_id: VIEWER_BINDING, label: 'Ada Lovelace', role: 'owner',
    created_at: SNAPSHOT.members[0].created_at }
  let finishRefresh
  const refresh = new Promise((resolve) => { finishRefresh = resolve })
  getProjectLifecycle.mockReset().mockResolvedValueOnce(initial).mockReturnValueOnce(refresh)
  getOrgIdentities.mockReset().mockResolvedValue({ identities: [
    { ...identity, label: 'Member aaaaaaaa' },
    { binding_id: SNAPSHOT.members[1].binding_id, label: 'Other member' },
  ] })
  setIdentityDisplayName.mockReset().mockResolvedValue({ identity })
  render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Load organization members' }))
  await screen.findByRole('option', { name: /^Member aaaaaaaa/ })
  expect(getOrgIdentities).toHaveBeenCalledTimes(1)
  fireEvent.change(screen.getByLabelText('Display name for Member aaaaaaaa'), { target: { value: 'Ada Lovelace' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Member aaaaaaaa' }))

  // Both views use the PUT row while the lifecycle read is still pending.
  await screen.findByRole('option', { name: /^Ada Lovelace/ })
  expect(screen.getByText('Ada Lovelace · joined 2026-08-01')).toBeTruthy()
  expect(screen.getByRole('option', { name: 'Other member' })).toBeTruthy()
  expect(screen.queryByRole('option', { name: /^Member aaaaaaaa/ })).toBeNull()
  fireEvent.change(screen.getByLabelText('Search organization members'), { target: { value: 'Ada' } })
  expect(screen.getByRole('option', { name: /^Ada Lovelace/ }).value).toBe(VIEWER_BINDING)
  expect(screen.queryByRole('option', { name: 'Other member' })).toBeNull()
  expect(getOrgIdentities).toHaveBeenCalledTimes(1)

  await act(async () => {
    finishRefresh({ ...initial, members: initial.members.map((member) =>
      member.binding_id === identity.binding_id ? { ...member, label: identity.label } : member) })
  })
  await waitFor(() => expect(screen.getByLabelText('Display name for Ada Lovelace')).not.toBeDisabled())
  expect(getProjectLifecycle).toHaveBeenCalledTimes(2)
  expect(getOrgIdentities).toHaveBeenCalledTimes(1)
  expect(setIdentityDisplayName).toHaveBeenCalledTimes(1)
})

it('B5 a failed refresh after a save keeps the saved name', async () => {
  const orgId = 'cccccccc-dddd-4eee-8fff-111111111111'
  const initial = {
    ...SNAPSHOT,
    project: { ...SNAPSHOT.project, org_id: orgId },
    viewer: { ...SNAPSHOT.viewer, can_label_identities: true },
    members: SNAPSHOT.members.map((member) => member.binding_id === VIEWER_BINDING
      ? { ...member, label: 'Ada Lovelace' } : member),
  }
  getProjectLifecycle.mockReset().mockResolvedValueOnce(initial)
    .mockRejectedValueOnce(new Error('The project could not be refreshed.'))
  getOrgIdentities.mockReset()
  setIdentityDisplayName.mockReset().mockResolvedValue({ identity: {
    binding_id: VIEWER_BINDING, label: 'Grace Hopper', role: 'owner',
    created_at: SNAPSHOT.members[0].created_at,
  } })
  render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
  fireEvent.change(await screen.findByLabelText('Display name for Ada Lovelace'), { target: { value: 'Grace Hopper' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Ada Lovelace' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The project could not be refreshed.')
  await waitFor(() => expect(screen.getByLabelText('Display name for Grace Hopper')).not.toBeDisabled())
  expect(screen.getByLabelText('Display name for Grace Hopper').value).toBe('Grace Hopper')
  expect(screen.getByText('Grace Hopper · joined 2026-08-01')).toBeTruthy()
  expect(screen.queryByLabelText('Display name for Ada Lovelace')).toBeNull()
  expect(getProjectLifecycle).toHaveBeenCalledTimes(2)
  expect(getOrgIdentities).not.toHaveBeenCalled()
  expect(setIdentityDisplayName).toHaveBeenCalledTimes(1)
  expect(setIdentityDisplayName).toHaveBeenCalledWith(orgId, VIEWER_BINDING, 'Grace Hopper')
})

it('B5 a failed save keeps the draft', async () => {
  const orgId = 'cccccccc-dddd-4eee-8fff-111111111111'
  getProjectLifecycle.mockReset().mockResolvedValue({
    ...SNAPSHOT,
    project: { ...SNAPSHOT.project, org_id: orgId },
    viewer: { ...SNAPSHOT.viewer, can_label_identities: true },
    members: SNAPSHOT.members.map((member) => member.binding_id === VIEWER_BINDING
      ? { ...member, label: 'Ada Lovelace' } : member),
  })
  setIdentityDisplayName.mockReset().mockRejectedValue(new Error('The name could not be saved.'))
  render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
  fireEvent.change(await screen.findByLabelText('Display name for Ada Lovelace'), { target: { value: '  Grace Hopper  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save display name for Ada Lovelace' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The name could not be saved.')
  expect(screen.getByLabelText('Display name for Ada Lovelace').value).toBe('  Grace Hopper  ')
  expect(screen.getByLabelText('Display name for Ada Lovelace')).not.toBeDisabled()
  expect(screen.getByText('Ada Lovelace · joined 2026-08-01')).toBeTruthy()
  expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
  expect(setIdentityDisplayName).toHaveBeenCalledTimes(1)
  expect(setIdentityDisplayName).toHaveBeenCalledWith(orgId, VIEWER_BINDING, 'Grace Hopper')
})

describe('lifecycle block renders for an open project with the flag on', () => {
  it('mounts membership, the timeline, the danger zone, and the clone/export affordances', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} projectName="Rooftop Array" />)

    await waitFor(() => expect(screen.getByTestId('membership-panel')).toBeTruthy())
    expect(screen.getByTestId('projects-surface')).toBeTruthy()
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1) // ONE read feeds every child
    expect(getProjectLifecycle).toHaveBeenCalledWith(PROJECT_ID)

    // Roster comes from the snapshot verbatim, with the wire's `read_only`
    // rendered in the component's own `read-only` vocabulary.
    // w4h-b4 replaces raw identity mechanics with labels and scoped choices.
    expect(screen.getByLabelText('Role for Member aaaaaaaa').value).toBe('owner')
    expect(screen.getByLabelText('Role for Member bbbbbbbb').value).toBe('read-only')
    expect(screen.getByText('Member aaaaaaaa · joined 2026-08-01')).toBeTruthy()
    expect(screen.getByText('Member bbbbbbbb · joined 2026-08-02')).toBeTruthy()
    expect(screen.getByText('Organization members are unavailable. No one can be invited yet.')).toBeTruthy()

    expect(screen.getByRole('region', { name: 'Project timeline' })).toBeTruthy()
    expect(screen.getByText('project_created')).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Danger zone' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^clone project$/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^export project$/i })).toBeTruthy()

    // Reset and delete are TERMINAL on this platform (no restore token is
    // minted), so no undo may be offered.
    expect(screen.queryByText(/undo/i)).toBeNull()
  })

  // REGRESSION GUARD. The first cut of this graft derived viewer identity from
  // a localStorage key (`leaf.actor_binding_id`) that nothing in the web tree
  // ever wrote, so `authority` was always null and Membership.jsx - which
  // refuses to guess a role matrix - rendered an EMPTY DIV in every
  // deployment. The role matrix must come from the snapshot's `viewer`.
  it('renders the role matrix from the snapshot viewer, not from client-side storage', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} projectName="Rooftop Array" />)

    await waitFor(() => expect(screen.getByText(/your role: owner/i)).toBeTruthy())
    expect(screen.getByText('Organization members are unavailable. No one can be invited yet.')).toBeTruthy()
  })

  it('renders no role matrix when the server sends no viewer', async () => {
    const { viewer, ...noViewer } = SNAPSHOT
    getProjectLifecycle.mockResolvedValue(noViewer)
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} projectName="Rooftop Array" />)

    // The surface still mounts (timeline, danger zone, clone/export), but the
    // membership matrix stays absent rather than guessed.
    await waitFor(() => expect(screen.getByTestId('projects-surface')).toBeTruthy())
    expect(screen.queryByText(/your role:/i)).toBeNull()
  })

  it('renders nothing at all with the flag off, and never touches the transport', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const { container } = render(
      <ProjectLifecyclePanel enabled={false} projectId={PROJECT_ID} projectName="Rooftop Array" />,
    )
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByTestId('projects-surface')).toBeNull()
    expect(screen.queryByTestId('membership-panel')).toBeNull()
    expect(getProjectLifecycle).not.toHaveBeenCalled()
  })

  it('renders nothing when no project is open', () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const { container } = render(<ProjectLifecyclePanel enabled projectId={null} />)
    expect(container).toBeEmptyDOMElement()
    expect(getProjectLifecycle).not.toHaveBeenCalled()
  })
})

describe('w4h-b4 lifecycle results and scoped identities', () => {
  it('B4-c row1 loads organization members on demand with the ToolCast props', async () => {
    getProjectLifecycle.mockResolvedValue({ ...SNAPSHOT, project: { ...SNAPSHOT.project, org_id: 'org-a' } })
    getOrgIdentities.mockResolvedValue({ identities: [
      { binding_id: 'binding-alex', label: 'Alex' },
      { binding_id: 'binding-sam', label: 'Sam' },
    ] })
    render(<ProjectLifecyclePanel projectId={PROJECT_ID} projectName="Rooftop Array" onProjectDeleted={vi.fn()} />)
    const load = await screen.findByRole('button', { name: 'Load organization members' })
    expect(getOrgIdentities).not.toHaveBeenCalled()
    fireEvent.click(load)
    await screen.findByRole('option', { name: 'Alex' })
    expect(screen.getByRole('option', { name: 'Sam' }).value).toBe('binding-sam')
    expect(getOrgIdentities).toHaveBeenCalledWith('org-a')
  })

  it('B4-c row2 lets parent identities override the hook until the override is removed', async () => {
    getProjectLifecycle.mockResolvedValue({ ...SNAPSHOT, project: { ...SNAPSHOT.project, org_id: 'org-a' } })
    getOrgIdentities.mockResolvedValue({ identities: [{ binding_id: 'hook-binding', label: 'Hook member' }] })
    const { rerender } = render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID}
      identities={[{ binding_id: 'parent-binding', label: 'Parent member' }]} />)
    expect((await screen.findByRole('option', { name: 'Parent member' })).value).toBe('parent-binding')
    expect(screen.queryByRole('button', { name: 'Load organization members' })).toBeNull()
    expect(getOrgIdentities).not.toHaveBeenCalled()
    rerender(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
    fireEvent.click(screen.getByRole('button', { name: 'Load organization members' }))
    await screen.findByRole('option', { name: 'Hook member' })
    expect(screen.queryByRole('option', { name: 'Parent member' })).toBeNull()
  })

  it('B4-c row5 drops the remembered clone receipt once a refresh carries it', async () => {
    const receipt = { receipt_id: 'clone-remembered', action: 'project_cloned', created_at: '2026-09-17T00:00:00Z' }
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    cloneProject.mockResolvedValue({ project: { project_id: 'copy', name: 'Copy' }, receipt })
    inviteMember.mockResolvedValue({})
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
    await screen.findByTestId('membership-panel')
    fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
    await screen.findByText(/Clone complete:/)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    expect(screen.getAllByText('Receipt: clone-remembered')).toHaveLength(1)

    getProjectLifecycle.mockResolvedValue({ ...SNAPSHOT, receipts: [...SNAPSHOT.receipts, receipt] })
    fireEvent.change(screen.getByLabelText('Role for Member bbbbbbbb'), { target: { value: 'editor' } })
    await waitFor(() => expect(getProjectLifecycle).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(screen.getByLabelText('Role for Member bbbbbbbb')).not.toBeDisabled())
    expect(screen.getAllByText('Receipt: clone-remembered')).toHaveLength(1)

    // A later server window omits it. A retained local copy would resurrect it.
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    fireEvent.change(screen.getByLabelText('Role for Member bbbbbbbb'), { target: { value: 'reviewer' } })
    await waitFor(() => expect(getProjectLifecycle).toHaveBeenCalledTimes(4))
    await waitFor(() => expect(screen.queryByText('Receipt: clone-remembered')).toBeNull())
  })

  it('retains the clone receipt after closing its dialog even when refresh omits it', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    cloneProject.mockResolvedValue({ project: { project_id: 'copy', name: 'Rooftop Array (copy)' },
      receipt: { receipt_id: 'clone-receipt', action: 'project_cloned', created_at: '2026-09-17T00:00:00Z' } })
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
    await screen.findByTestId('membership-panel')
    fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
    await screen.findByText(/Clone complete:/)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(within(screen.getByRole('region', { name: 'Project timeline' })).getByText('Receipt: clone-receipt')).toBeTruthy()
  })

  it('passes scoped identities and delegates their load to its parent', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const onLoadIdentities = vi.fn().mockResolvedValue(undefined)
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID}
      identities={[{ binding_id: 'binding-a', label: 'Alex' }]} onLoadIdentities={onLoadIdentities} />)
    await screen.findByLabelText('Invite member')
    expect(screen.getByRole('option', { name: 'Alex' }).value).toBe('binding-a')
    fireEvent.click(screen.getByRole('button', { name: 'Load organization members' }))
    await waitFor(() => expect(onLoadIdentities).toHaveBeenCalledTimes(1))
  })

  it('retains the export receipt after its dialog closes', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    exportProject.mockResolvedValue({ receipt: { receipt_id: 'export-receipt', action: 'project_exported', created_at: '2026-09-17T00:00:00Z' } })
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} />)
    await screen.findByTestId('membership-panel')
    fireEvent.click(screen.getByRole('button', { name: 'Export project' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Export' }))
    await within(screen.getByRole('dialog')).findByText('export-receipt')
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    expect(screen.getByText('Receipt: export-receipt')).toBeTruthy()
  })

  it.each(['Reset', 'Delete'])('retains the %s receipt when its confirmation closes', async (action) => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const operation = action === 'Reset' ? resetProject : deleteProject
    operation.mockResolvedValue({ receipt: { receipt_id: 'danger-receipt', action: `project_${action.toLowerCase()}`, created_at: '2026-09-17T00:00:00Z' } })
    const onProjectDeleted = vi.fn()
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} onProjectDeleted={onProjectDeleted} />)
    await screen.findByTestId('membership-panel')
    fireEvent.click(screen.getByRole('button', { name: action }))
    fireEvent.change(screen.getByLabelText(`Type the project name to confirm ${action.toLowerCase()}`), { target: { value: 'Rooftop Array' } })
    fireEvent.click(screen.getByRole('button', { name: action }))
    const timeline = screen.getByRole('region', { name: 'Project timeline' })
    await within(timeline).findByText('Receipt: danger-receipt')
    expect(screen.queryByLabelText(`Type the project name to confirm ${action.toLowerCase()}`)).toBeNull()
    if (action === 'Delete') expect(onProjectDeleted).toHaveBeenCalledWith(PROJECT_ID, 'danger-receipt')
  })

  it('exposes drawing files from the lifecycle read without adapting their contents', async () => {
    const files = [{ file_id: 'drawing-a', name: 'Roof.dxf' }]
    getProjectLifecycle.mockResolvedValue({ ...SNAPSHOT, files })
    const { result } = renderHook(() => useProjectLifecycle(PROJECT_ID))
    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.files).toEqual(files)
  })
})

describe('ToolCast mounts the lifecycle block behind the exact ratified gate', () => {
  // vitest's root is web/ (vitest.config.js), and import.meta.url is not a
  // file: URL under the jsdom transform, so the path is resolved from the root.
  const source = readFileSync(join(process.cwd(), 'src', 'site', 'ToolCast.jsx'), 'utf8')

  it('gates the panel on the build flag FIRST, then the demo/mock/operator fences and an open project', () => {
    const gate = source.match(
      /\{ENV_LIFECYCLE_UI && leftView === 'workspace'[\s\S]{0,200}?<ProjectLifecyclePanel/,
    )
    expect(gate).toBeTruthy()
    const [text] = gate
    for (const fence of ['!PUBLIC_DEMO', '!transportMock', 'canOperate', 'workspace.openProjectId']) {
      expect(text).toContain(fence)
    }
  })

  it('is the only ProjectLifecyclePanel mount, and ProjectList is gone from the tree', () => {
    expect(source.match(/<ProjectLifecyclePanel/g).length).toBe(1)
    expect(source).not.toContain('ProjectList')
  })

  // The blank-project factory mints the owner membership binding the lifecycle
  // routes need, but it goes through _get_lifecycle_actor, which REQUIRES
  // X-Actor-Binding-Id whenever LEAF_AUTH_LIVE is off (the default). No browser
  // can produce that id, so routing creation there breaks creation in every
  // auth-off deployment, flag on OR off, because workspaceServices is a
  // module-level const and is not behind the flag.
  it('leaves project creation on the permissive route, flag-independent', () => {
    expect(source).toContain('const workspaceServices = { createOrg, listProjects, createProject, openProject }')
    expect(source).not.toContain('createProject: createBlankProject')
    expect(source).not.toContain('createBlankProject')
  })
})
