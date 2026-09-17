import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { authConfigured } from '../../auth.js'
import { isWorkspaceBootstrapRequired } from '../../api.js'
import { createWorkspaceController } from './createWorkspaceController.js'
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

it('uses the API bootstrap predicate to publish an unbound identity', async () => {
  const error = { status: 403, body: { detail: 'A binding error recognized by the API helper.' } }
  isWorkspaceBootstrapRequired.mockReturnValue(true)
  const services = { listProjects: vi.fn().mockRejectedValue(error) }
  const storage = { getItem: vi.fn(() => null), removeItem: vi.fn() }
  const { result } = renderHook(() => useWorkspaceController({ services, storage }))
  expect(createWorkspaceController).toHaveBeenCalledWith(expect.objectContaining({
    authLive: authConfigured, isBootstrapRequired: isWorkspaceBootstrapRequired,
  }))
  await waitFor(() => expect(result.current.bootstrapState).toBe('unbound'))
  expect(isWorkspaceBootstrapRequired).toHaveBeenCalledWith(error)
  expect(result.current.orgId).toBeNull()
})
