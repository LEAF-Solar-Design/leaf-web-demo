import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { authConfigured } from '../../auth.js'
import { isWorkspaceBootstrapRequired } from '../../api.js'
import { createWorkspaceController, WORKSPACE_ORG_KEY } from './createWorkspaceController.js'
import useWorkspaceController from './useWorkspaceController.js'

vi.mock('../../auth.js', () => ({ authConfigured: true }))
vi.mock('../../api.js', () => ({ isWorkspaceBootstrapRequired: vi.fn() }))
vi.mock('./createWorkspaceController.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, createWorkspaceController: vi.fn(actual.createWorkspaceController) }
})

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

it('passes configured auth to the controller and lists without a cached org', async () => {
  const services = { listProjects: vi.fn().mockResolvedValue([]) }
  const storage = { getItem: vi.fn(() => null) }
  const { result } = renderHook(() => useWorkspaceController({ services, storage }))
  expect(createWorkspaceController).toHaveBeenCalledWith(expect.objectContaining({ authLive: authConfigured }))
  await waitFor(() => expect(result.current.bootstrapState).toBe('bound'))
  expect(services.listProjects).toHaveBeenCalledWith(null)
})

it('B1-d row7: the hook clears the cached org and settles its follow-up unbound read', async () => {
  const error = { status: 403, body: { detail: 'A binding error recognized by the API helper.' } }
  isWorkspaceBootstrapRequired.mockReturnValue(true)
  const services = { listProjects: vi.fn().mockRejectedValue(error) }
  let storedId = 'old-org'
  const storage = { getItem: vi.fn(() => storedId), removeItem: vi.fn(() => { storedId = null }) }
  const { result } = renderHook(() => useWorkspaceController({ services, storage }))
  expect(createWorkspaceController).toHaveBeenCalledWith(expect.objectContaining({
    authLive: authConfigured, isBootstrapRequired: isWorkspaceBootstrapRequired,
  }))
  await waitFor(() => expect(result.current.bootstrapState).toBe('unbound'))
  await waitFor(() => expect(services.listProjects).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(result.current.projectsLoading).toBe(false))
  expect(result.current.bootstrapState).toBe('unbound')
  expect(services.listProjects.mock.calls).toEqual([['old-org'], [null]])
  expect(storage.removeItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY)
  expect(storage.getItem(WORKSPACE_ORG_KEY)).toBeNull()
  expect(result.current.projectsError).toBeNull()
  expect(result.current.bootstrapMessage).toEqual(expect.any(String))
  expect(isWorkspaceBootstrapRequired).toHaveBeenCalledWith(error)
  expect(result.current.orgId).toBeNull()
})
