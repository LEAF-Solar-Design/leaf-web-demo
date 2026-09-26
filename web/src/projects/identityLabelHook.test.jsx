import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
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

import { getOrgIdentities, getProjectLifecycle, setIdentityDisplayName } from './api.js'
import useProjectLifecycle from './useProjectLifecycle.js'

const ORG_ID = '11111111-2222-4333-8444-555555555555'
const PROJECT_ID = '22222222-2222-4333-8444-555555555555'
const BINDING_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
const snapshot = {
  project: { project_id: PROJECT_ID, org_id: ORG_ID },
  viewer: { role: 'owner', membership_id: 'member-1', can_label_identities: true },
  members: [{ membership_id: 'member-1', binding_id: BINDING_ID, role: 'owner', label: 'Member aaaaaaaa' }],
  files: [], receipts: [],
}

beforeEach(() => {
  vi.resetAllMocks()
  getProjectLifecycle.mockResolvedValue(snapshot)
})
afterEach(cleanup)

function deferred() {
  let resolve
  const promise = new Promise((finish) => { resolve = finish })
  return { promise, resolve }
}

function namedSnapshot(label, projectId = PROJECT_ID) {
  return {
    ...snapshot,
    project: { ...snapshot.project, project_id: projectId },
    members: [{ ...snapshot.members[0], label }],
  }
}

it('B5 the hook reads can_label from the server viewer', async () => {
  const { result } = renderHook(() => useProjectLifecycle(PROJECT_ID))
  await waitFor(() => expect(result.current.status).toBe('ready'))
  expect(result.current.authority.can_label).toBe(true)
  for (const canLabel of [false, undefined]) {
    getProjectLifecycle.mockResolvedValue({
      ...snapshot, viewer: { ...snapshot.viewer, can_label_identities: canLabel },
    })
    await act(async () => { await result.current.refetch() })
    expect(result.current.authority.can_label).toBe(false)
  }
})

it('B5 setLabel writes through the org id and member binding then refetches', async () => {
  let finishWrite
  setIdentityDisplayName.mockReturnValue(new Promise((resolve) => { finishWrite = resolve }))
  const { result } = renderHook(() => useProjectLifecycle(PROJECT_ID))
  await waitFor(() => expect(result.current.status).toBe('ready'))
  let pending
  act(() => { pending = result.current.actions.setLabel('member-1', 'Ada Lovelace') })
  expect(setIdentityDisplayName).toHaveBeenCalledWith(ORG_ID, BINDING_ID, 'Ada Lovelace')
  expect(getProjectLifecycle).toHaveBeenCalledTimes(1)
  getProjectLifecycle.mockResolvedValue({
    ...snapshot, members: [{ ...snapshot.members[0], label: 'Ada Lovelace' }],
  })
  await act(async () => {
    finishWrite({ identity: { label: 'Ada Lovelace' } })
    await pending
  })
  expect(getProjectLifecycle).toHaveBeenCalledTimes(2)
  expect(result.current.members[0].label).toBe('Ada Lovelace')
})

it('B5 a save drops an identities read that started before it', async () => {
  const staleRead = deferred()
  const ada = { binding_id: BINDING_ID, label: 'Ada' }
  getProjectLifecycle.mockResolvedValue(namedSnapshot('Ada'))
  getOrgIdentities.mockResolvedValueOnce({ identities: [ada] }).mockReturnValueOnce(staleRead.promise)
  setIdentityDisplayName.mockResolvedValue({ identity: { ...ada, label: 'Grace' } })
  const { result } = renderHook(() => useProjectLifecycle(PROJECT_ID))
  await waitFor(() => expect(result.current.status).toBe('ready'))
  await act(async () => { await result.current.loadIdentities() })
  let pendingRead
  act(() => { pendingRead = result.current.loadIdentities() })
  expect(result.current.identitiesStatus).toBe('loading')
  getProjectLifecycle.mockResolvedValue(namedSnapshot('Grace'))
  await act(async () => { await result.current.actions.setLabel('member-1', 'Grace') })
  expect(result.current.identities[0].label).toBe('Grace')
  await act(async () => {
    staleRead.resolve({ identities: [ada] })
    await pendingRead
  })
  expect(result.current.identities[0].label).toBe('Grace')
  expect(result.current.members[0].label).toBe('Grace')
  expect(result.current.identitiesStatus).not.toBe('loading')
})

it('B5 a save that resolves after a project switch changes nothing in the new project', async () => {
  const save = deferred()
  const otherProjectId = '33333333-2222-4333-8444-555555555555'
  getProjectLifecycle.mockImplementation(async (id) => namedSnapshot(id === PROJECT_ID ? 'Ada' : 'B member', id))
  getOrgIdentities.mockResolvedValue({ identities: [{ binding_id: BINDING_ID, label: 'B member' }] })
  setIdentityDisplayName.mockReturnValue(save.promise)
  const { result, rerender } = renderHook(({ projectId }) => useProjectLifecycle(projectId), {
    initialProps: { projectId: PROJECT_ID },
  })
  await waitFor(() => expect(result.current.status).toBe('ready'))
  let pendingSave
  act(() => { pendingSave = result.current.actions.setLabel('member-1', 'Grace') })
  rerender({ projectId: otherProjectId })
  await waitFor(() => expect(result.current.project?.project_id).toBe(otherProjectId))
  await act(async () => { await result.current.loadIdentities() })
  const members = result.current.members
  const identities = result.current.identities
  const reads = getProjectLifecycle.mock.calls.length
  await act(async () => {
    save.resolve({ identity: { binding_id: BINDING_ID, label: 'Grace' } })
    await pendingSave
  })
  expect(getProjectLifecycle).toHaveBeenCalledTimes(reads)
  expect(result.current.project.project_id).toBe(otherProjectId)
  expect(result.current.members).toBe(members)
  expect(result.current.identities).toBe(identities)
  expect(result.current.identitiesStatus).toBe('ready')
  expect(result.current.refreshing).toBe(false)
})

it('B5 an older save response never overwrites a newer saved label', async () => {
  const olderSave = deferred()
  const otherProjectId = '33333333-2222-4333-8444-555555555555'
  getProjectLifecycle.mockImplementation(async (id) => namedSnapshot('Ada', id))
  getOrgIdentities.mockResolvedValue({ identities: [{ binding_id: BINDING_ID, label: 'Ada' }] })
  setIdentityDisplayName.mockReturnValueOnce(olderSave.promise)
    .mockResolvedValueOnce({ identity: { binding_id: BINDING_ID, label: 'Katherine' } })
  const { result, rerender } = renderHook(({ projectId }) => useProjectLifecycle(projectId), {
    initialProps: { projectId: PROJECT_ID },
  })
  await waitFor(() => expect(result.current.status).toBe('ready'))
  let pendingOlderSave
  act(() => { pendingOlderSave = result.current.actions.setLabel('member-1', 'Grace') })
  rerender({ projectId: otherProjectId })
  await waitFor(() => expect(result.current.project?.project_id).toBe(otherProjectId))
  rerender({ projectId: PROJECT_ID })
  await waitFor(() => expect(result.current.project?.project_id).toBe(PROJECT_ID))
  await act(async () => { await result.current.loadIdentities() })
  getProjectLifecycle.mockResolvedValue(namedSnapshot('Katherine'))
  await act(async () => { await result.current.actions.setLabel('member-1', 'Katherine') })
  expect(result.current.members[0].label).toBe('Katherine')
  expect(result.current.identities[0].label).toBe('Katherine')
  const reads = getProjectLifecycle.mock.calls.length
  const members = result.current.members
  const identities = result.current.identities
  await act(async () => {
    olderSave.resolve({ identity: { binding_id: BINDING_ID, label: 'Grace' } })
    await pendingOlderSave
  })
  expect(getProjectLifecycle).toHaveBeenCalledTimes(reads)
  expect(result.current.members).toBe(members)
  expect(result.current.identities).toBe(identities)
  expect(result.current.members[0].label).toBe('Katherine')
  expect(result.current.identities[0].label).toBe('Katherine')
})
