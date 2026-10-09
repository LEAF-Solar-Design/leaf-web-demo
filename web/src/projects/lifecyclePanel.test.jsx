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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect, useState } from 'react'
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

import { cloneProject, deleteProject, exportProject, getOrgIdentities, getProjectLifecycle, inviteMember, resetProject, revokeMember, setIdentityDisplayName } from './api.js'
import { renderHook } from '@testing-library/react'
import useProjectLifecycle from './useProjectLifecycle.js'
import ProjectLifecyclePanel from './ProjectLifecyclePanel.jsx'
import ProjectWorkspacePanels, { deriveBoardPaneSeats, PersistentSeat } from '../workspace/ProjectWorkspacePanels.jsx'
import { ProjectBoardGround } from '../site/ProjectBoardGround.jsx'

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

  it('ToolCast is the only ProjectLifecyclePanel mount in its source, and ProjectList is gone', () => {
    expect(source.match(/<ProjectLifecyclePanel/g).length).toBe(1)
    expect(source).not.toContain('ProjectList')
  })

  it('BI03-21 ToolCast retains its lifecycle mount and fences', () => {
    expect(source.match(/<ProjectLifecyclePanel/g).length).toBe(1)
    expect(source).not.toContain('ProjectList')
    const mount = source.slice(source.indexOf("{ENV_LIFECYCLE_UI && leftView === 'workspace'"))
      .split('/>')[0]
    for (const fence of ['!PUBLIC_DEMO', '!transportMock', 'canOperate', 'workspace.openProjectId']) {
      expect(mount).toContain(fence)
    }
    for (const prop of ['projectId={workspace.openProjectId}', 'projectName={currentProjectName}',
      'onProjectDeleted={forgetDeletedProject}']) expect(mount).toContain(prop)
    expect(source).toContain('const workspaceServices = { createOrg, listProjects, createProject, openProject }')
    expect(source).not.toContain('createBlankProject')
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

// The real owner stays at one React position; pane and surface changes move
// the production PersistentSeat's container rather than reconstructing it.
function ObservedLifecycle({ counts, ...props }) {
  useEffect(() => {
    counts.mounts += 1
    return () => { counts.unmounts += 1 }
  }, [counts])
  return <ProjectLifecyclePanel {...props} />
}

function LifecycleBoard({ counts, onProjectDeleted, surface = 'board', initialPane = null, ...patch }) {
  const [pane, setPane] = useState(initialPane)
  const [destination, setDestination] = useState(null)
  const inputs = {
    lifecycleEnabled: true, mock: false, signedIn: true, sessionStatus: 'active',
    projectId: PROJECT_ID, drawingId: null, canonicalVersionId: null,
    sessionId: null, canConverse: false, agentMode: null,
    ...patch, boardHostsProject: surface === 'board', projectPane: pane,
    settingsDestination: destination,
  }
  const seats = deriveBoardPaneSeats(inputs)
  return <>
    {surface === 'board' && <ProjectBoardGround active worldSpace={false}
      actions={{ onOpenSettings: seats.lifecycleEligible ? () => setPane('settings') : undefined,
        onOpenCapability: setPane }}
      panel={<ProjectWorkspacePanels pane={pane} onBack={() => setPane(null)}
        slots={{ settings: seats.settingsReason ? <p>{seats.settingsReason}</p> : <div ref={setDestination} /> }} />} />}
    {inputs.lifecycleEnabled && seats.lifecycleEligible &&
      <PersistentSeat key={inputs.projectId} destination={seats.settingsTarget}>
        <ObservedLifecycle counts={counts} projectId={inputs.projectId}
          projectName={inputs.projectId === PROJECT_ID ? 'Rooftop Array' : 'Project B'}
          onProjectDeleted={onProjectDeleted} />
      </PersistentSeat>}
  </>
}

describe('board Settings lifecycle seating', () => {
  beforeEach(() => {
    for (const transport of [getProjectLifecycle, getOrgIdentities, cloneProject, exportProject,
      deleteProject, inviteMember, revokeMember, resetProject, setIdentityDisplayName]) transport.mockReset()
    getProjectLifecycle.mockResolvedValue({ ...SNAPSHOT, project: { ...SNAPSHOT.project, org_id: 'org-a' } })
    getOrgIdentities.mockResolvedValue({ identities: [{ binding_id: 'binding-new', label: 'New member' }] })
  })
  afterEach(() => vi.unstubAllGlobals())

  function mountBoard(patch = {}) {
    const counts = { mounts: 0, unmounts: 0 }
    const deleted = vi.fn()
    const view = render(<LifecycleBoard counts={counts} onProjectDeleted={deleted} {...patch} />)
    return { ...view, counts, deleted,
      update: (next) => view.rerender(<LifecycleBoard counts={counts} onProjectDeleted={deleted} {...patch} {...next} />) }
  }
  async function openSettings() {
    fireEvent.click(screen.getByRole('button', { name: 'Project settings' }))
    return screen.findByTestId('membership-panel')
  }
  function leaveSettings() {
    fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
  }
  function openDeleteDraft(value = 'partial project name') {
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const input = screen.getByLabelText('Type the project name to confirm delete')
    fireEvent.change(input, { target: { value } })
    return input
  }
  function expectSilent() {
    expect(getProjectLifecycle).not.toHaveBeenCalled()
    expect(getOrgIdentities).not.toHaveBeenCalled()
    expect(screen.queryByTestId('projects-surface')).toBeNull()
    for (const name of ['Clone project', 'Export project', 'Delete', 'Reset']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  }

  it('BI03-02 Settings contains the real lifecycle surface', async () => {
    mountBoard()
    await openSettings()
    expect(screen.getByTestId('projects-surface').closest('[data-pane="settings"]')).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Membership' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Project timeline' })).toBeTruthy()
    for (const name of ['Clone project', 'Export project', 'Reset', 'Delete']) {
      expect(screen.getByRole('button', { name })).not.toBeDisabled()
    }
    expect(screen.queryByText('Settings is not mounted on this surface.')).toBeNull()
  })

  it('BI03-03 An open project needs no drawing or conversation', async () => {
    const view = mountBoard()
    // The owner loads while still detached, before the first visit to Settings.
    await waitFor(() => expect(getProjectLifecycle).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('projects-surface')).toBeNull()
    await openSettings()
    expect(getProjectLifecycle).toHaveBeenCalledWith(PROJECT_ID)
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
    expect(screen.getByLabelText('Role for Member bbbbbbbb')).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke Member bbbbbbbb' })).not.toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Load organization members' }))
    await screen.findByRole('option', { name: 'New member' })
    expect(screen.getByRole('button', { name: 'Clone project' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Export project' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete' })).not.toBeDisabled()
  })

  it('BI03-04 Flag off leaves lifecycle unmounted and silent', () => {
    const view = mountBoard({ lifecycleEnabled: false, initialPane: 'settings' })
    expectSilent()
    expect(view.counts.mounts).toBe(0)
    expect(screen.getByText('Project settings are unavailable in this build.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Project settings' })).toBeNull()
  })

  it('BI03-06 Ineligible contexts perform no lifecycle transport', () => {
    for (const [patch, reason] of [
      [{ mock: true }, 'Project settings are unavailable in this offline demo.'],
      [{ signedIn: false }, 'Sign in to use project settings.'],
      [{ sessionStatus: 'required' }, 'Start an active session to use project settings.'],
      [{ projectId: null }, 'Open a project to use project settings.'],
    ]) {
      const view = mountBoard({ ...patch, initialPane: 'settings' })
      expectSilent()
      expect(view.counts.mounts).toBe(0)
      expect(screen.getByText(reason)).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Project settings' })).toBeNull()
      view.unmount()
    }
  })

  it('BI03-07 Pane changes preserve the lifecycle owner and draft', async () => {
    const view = mountBoard()
    await openSettings()
    const panel = screen.getByTestId('projects-surface')
    const container = panel.parentElement
    fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
    const dialog = screen.getByRole('dialog')
    const draft = openDeleteDraft()
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation' }))
    expect(container.isConnected).toBe(false)
    expect(screen.queryByTestId('projects-surface')).toBeNull()
    await openSettings()
    expect(screen.getByTestId('projects-surface')).toBe(panel)
    expect(panel.parentElement).toBe(container)
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(screen.getByLabelText('Type the project name to confirm delete')).toBe(draft)
    expect(draft.value).toBe('partial project name')
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
  })

  it('BI03-08 Surface changes detach and reseat one lifecycle owner', async () => {
    const view = mountBoard()
    await openSettings()
    const panel = screen.getByTestId('projects-surface')
    const container = panel.parentElement
    openDeleteDraft('surface draft')
    view.update({ surface: 'other' })
    expect(container.isConnected).toBe(false)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
    view.update({ surface: 'board' })
    await screen.findByTestId('membership-panel')
    expect(container.isConnected).toBe(true)
    expect(screen.getAllByTestId('projects-surface')).toEqual([panel])
    expect(panel.parentElement).toBe(container)
    expect(screen.getByLabelText('Type the project name to confirm delete').value).toBe('surface draft')
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
  })

  it('BI03-09 Project changes discard prior dialog and receipt state', async () => {
    const view = mountBoard()
    await openSettings()
    const panelA = screen.getByTestId('projects-surface')
    const containerA = panelA.parentElement
    fireEvent.click(screen.getByRole('button', { name: 'Load organization members' }))
    await screen.findByRole('option', { name: 'New member' })
    cloneProject.mockResolvedValue({ project: { project_id: 'copy-a', name: 'Copy A' },
      receipt: { receipt_id: 'receipt-a', action: 'project_cloned', created_at: '2026-10-06T00:00:00Z' } })
    fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
    await screen.findByText(/Clone complete:/)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    openDeleteDraft('A draft')
    let resolveA
    getProjectLifecycle.mockImplementation((id) => id === PROJECT_ID
      ? new Promise((resolve) => { resolveA = resolve })
      : Promise.resolve({ ...SNAPSHOT, project: { project_id: 'B', name: 'Project B', org_id: 'org-b' }, receipts: [] }))
    inviteMember.mockResolvedValue({})
    fireEvent.change(screen.getByLabelText('Role for Member bbbbbbbb'), { target: { value: 'editor' } })
    await waitFor(() => expect(resolveA).toBeTypeOf('function'))
    view.update({ projectId: 'B' })
    await screen.findByTestId('membership-panel')
    expect(screen.getByTestId('projects-surface')).not.toBe(panelA)
    expect(containerA.isConnected).toBe(false)
    expect(view.counts).toEqual({ mounts: 2, unmounts: 1 })
    expect(getProjectLifecycle).toHaveBeenCalledWith('B')
    expect(screen.queryByLabelText('Type the project name to confirm delete')).toBeNull()
    expect(screen.queryByText('Receipt: receipt-a')).toBeNull()
    expect(screen.queryByRole('option', { name: 'New member' })).toBeNull()
    await act(async () => { resolveA({ ...SNAPSHOT, project: { ...SNAPSHOT.project, name: 'Late A' } }) })
    expect(screen.queryByText('Late A')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByLabelText('Type the project name to confirm delete').value).toBe('')
    expect(screen.getByText('Project B', { selector: 'strong' })).toBeTruthy()
  })

  it('BI03-10 Losing eligibility unmounts the lifecycle owner', async () => {
    for (const patch of [{ mock: true }, { signedIn: false }, { sessionStatus: 'required' }, { projectId: null }]) {
      getProjectLifecycle.mockClear()
      const view = mountBoard()
      await openSettings()
      const container = screen.getByTestId('projects-surface').parentElement
      view.update(patch)
      expect(container.isConnected).toBe(false)
      expect(screen.queryByTestId('projects-surface')).toBeNull()
      expect(view.counts).toEqual({ mounts: 1, unmounts: 1 })
      view.update({ ...patch, unrelated: 'rerender' })
      expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
      view.unmount()
    }
  })

  it('BI03-11 Membership mutation refetches the existing owner once', async () => {
    for (const kind of ['invite', 'role', 'revoke']) {
      getProjectLifecycle.mockReset().mockResolvedValue({ ...SNAPSHOT, project: { ...SNAPSHOT.project, org_id: 'org-a' } })
      inviteMember.mockReset().mockResolvedValue({})
      revokeMember.mockReset().mockResolvedValue({})
      const view = mountBoard()
      await openSettings()
      const panel = screen.getByTestId('projects-surface')
      const updated = { ...SNAPSHOT, members: kind === 'revoke' ? [SNAPSHOT.members[0]]
        : kind === 'invite' ? [...SNAPSHOT.members, { membership_id: 'm-new', binding_id: 'binding-new',
          label: 'Invited server member', role: 'editor', status: 'active' }]
          : SNAPSHOT.members.map((member) => member.membership_id === 'm-2' ? { ...member, role: 'editor' } : member) }
      getProjectLifecycle.mockResolvedValue(updated)
      if (kind === 'invite') {
        fireEvent.click(screen.getByRole('button', { name: 'Load organization members' }))
        await screen.findByRole('option', { name: 'New member' })
        fireEvent.change(screen.getByLabelText('Invite member'), { target: { value: 'binding-new' } })
        fireEvent.change(screen.getByLabelText('Invite role'), { target: { value: 'editor' } })
        fireEvent.click(screen.getByRole('button', { name: 'Invite' }))
      } else if (kind === 'role') {
        fireEvent.change(screen.getByLabelText('Role for Member bbbbbbbb'), { target: { value: 'editor' } })
      } else fireEvent.click(screen.getByRole('button', { name: 'Revoke Member bbbbbbbb' }))
      await waitFor(() => expect(getProjectLifecycle).toHaveBeenCalledTimes(2))
      if (kind === 'revoke') {
        expect(revokeMember).toHaveBeenCalledTimes(1)
        expect(revokeMember).toHaveBeenCalledWith(PROJECT_ID, 'm-2')
        expect(inviteMember).not.toHaveBeenCalled()
        await waitFor(() => expect(screen.queryByLabelText('Role for Member bbbbbbbb')).toBeNull())
      } else {
        expect(inviteMember).toHaveBeenCalledTimes(1)
        expect(inviteMember).toHaveBeenCalledWith(PROJECT_ID,
          kind === 'invite' ? 'binding-new' : SNAPSHOT.members[1].binding_id, 'editor')
        expect(revokeMember).not.toHaveBeenCalled()
        await waitFor(() => expect(screen.getByLabelText(kind === 'invite'
          ? 'Role for Invited server member' : 'Role for Member bbbbbbbb').value).toBe('editor'))
      }
      expect(screen.getByTestId('projects-surface')).toBe(panel)
      expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
      view.unmount()
    }
  })

  it('BI03-12 Clone results survive pane changes', async () => {
    cloneProject.mockResolvedValue({ project: { project_id: 'copy-a', name: 'Server copy' },
      receipt: { receipt_id: 'clone-seat-receipt', action: 'project_cloned', created_at: '2026-10-06T00:00:00Z' } })
    const view = mountBoard()
    await openSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clone project' }))
    await within(dialog).findByText(/Clone complete: "Server copy"/)
    leaveSettings()
    await openSettings()
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(within(dialog).getByText('clone-seat-receipt')).toBeTruthy()
    expect(screen.getByText('Receipt: clone-seat-receipt')).toBeTruthy()
    expect(cloneProject).toHaveBeenCalledTimes(1)
    expect(cloneProject).toHaveBeenCalledWith(PROJECT_ID, 'Rooftop Array (copy)')
    expect(getProjectLifecycle).toHaveBeenCalledTimes(2)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
  })

  it('BI03-13 Export results survive pane changes', async () => {
    const createObjectURL = vi.fn(() => 'blob:settings-export')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
    exportProject.mockResolvedValue({ export: { schema: 'leaf.project-export.v1', project: { name: 'Rooftop Array' } },
      receipt: { receipt_id: 'export-seat-receipt', action: 'project_exported', created_at: '2026-10-06T00:00:00Z' } })
    const view = mountBoard()
    await openSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Export project' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Export' }))
    const download = await within(dialog).findByRole('link', { name: 'Download' })
    expect(download.getAttribute('href')).toBe('blob:settings-export')
    expect(download.getAttribute('download')).toBe('rooftop-array-export.json')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(createObjectURL.mock.calls[0][0]).toBeInstanceOf(Blob)
    leaveSettings()
    await openSettings()
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(within(dialog).getByText('export-seat-receipt')).toBeTruthy()
    expect(within(dialog).getByRole('link', { name: 'Download' })).toBe(download)
    expect(revokeObjectURL).not.toHaveBeenCalled()
    expect(exportProject).toHaveBeenCalledTimes(1)
    expect(exportProject).toHaveBeenCalledWith(PROJECT_ID, { signal: expect.any(AbortSignal) })
    expect(getProjectLifecycle).toHaveBeenCalledTimes(2)
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
    view.unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:settings-export')
  })

  it('BI03-15 Failed or cancelled deletion leaves the project open', async () => {
    const view = mountBoard()
    await openSettings()
    const panel = screen.getByTestId('projects-surface')
    openDeleteDraft('Rooftop Array')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(deleteProject).not.toHaveBeenCalled()
    expect(view.deleted).not.toHaveBeenCalled()
    deleteProject.mockRejectedValue(new Error('Deletion rejected by server'))
    openDeleteDraft('Rooftop Array')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await screen.findByText('Deletion rejected by server')
    expect(deleteProject).toHaveBeenCalledTimes(1)
    expect(deleteProject).toHaveBeenCalledWith(PROJECT_ID)
    expect(view.deleted).not.toHaveBeenCalled()
    expect(screen.getByTestId('projects-surface')).toBe(panel)
    expect(screen.getByLabelText('Type the project name to confirm delete').value).toBe('Rooftop Array')
    expect(view.counts).toEqual({ mounts: 1, unmounts: 0 })
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
  })
})
