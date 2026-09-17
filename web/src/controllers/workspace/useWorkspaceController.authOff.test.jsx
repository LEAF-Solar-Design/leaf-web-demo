import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { createWorkspaceController } from './createWorkspaceController.js'
import useWorkspaceController from './useWorkspaceController.js'

vi.mock('../../auth.js', () => ({ authConfigured: false }))
vi.mock('../../api.js', () => ({ isWorkspaceBootstrapRequired: vi.fn() }))
vi.mock('./createWorkspaceController.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, createWorkspaceController: vi.fn(actual.createWorkspaceController) }
})

afterEach(cleanup)

it('B1-d row6: auth-off reaches the controller and leaves an unbound cache miss idle', async () => {
  const services = { listProjects: vi.fn().mockResolvedValue([]) }
  const storage = { getItem: vi.fn(() => null) }
  const { result } = renderHook(() => useWorkspaceController({ services, storage }))
  expect(createWorkspaceController).toHaveBeenCalledWith(expect.objectContaining({ authLive: false }))
  await waitFor(() => expect(result.current.bootstrapState).toBe('unbound'))
  expect(result.current.bootstrapMessage).toBeNull()
  expect(result.current.projectsError).toBeNull()
  expect(result.current.projectsLoading).toBe(false)
  expect(services.listProjects).not.toHaveBeenCalled()
})
