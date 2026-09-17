import { afterEach, expect, it, vi } from 'vitest'
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
}))

import { getOrgIdentities, getProjectLifecycle } from './api.js'
import useProjectLifecycle from './useProjectLifecycle.js'

afterEach(cleanup)

it('B4-c row3 ignores a slow identities response after switching projects', async () => {
  let resolveIdentities
  getOrgIdentities.mockReturnValue(new Promise((resolve) => { resolveIdentities = resolve }))
  getProjectLifecycle.mockImplementation(async (projectId) => ({
    project: { project_id: projectId, org_id: `org-${projectId}` },
    members: [], receipts: [], files: [],
  }))
  const { result, rerender } = renderHook(({ projectId }) => useProjectLifecycle(projectId), {
    initialProps: { projectId: 'A' },
  })
  await waitFor(() => expect(result.current.status).toBe('ready'))
  let pending
  act(() => { pending = result.current.loadIdentities() })
  expect(result.current.identitiesStatus).toBe('loading')
  expect(getOrgIdentities).toHaveBeenCalledWith('org-A')
  rerender({ projectId: 'B' })
  await waitFor(() => expect(result.current.project?.project_id).toBe('B'))
  expect(result.current.identities).toBeNull()
  expect(result.current.identitiesStatus).toBe('idle')
  await act(async () => {
    resolveIdentities({ identities: [{ binding_id: 'a-member', label: 'A member' }] })
    await pending
  })
  expect(result.current.identities).toBeNull()
  expect(result.current.identitiesStatus).toBe('idle')
})
