import { StrictMode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { DEFAULT_WORKSPACE_NAME, createWorkspaceController } from './createWorkspaceController.js'
import useWorkspaceController from './useWorkspaceController.js'

vi.mock('../../auth.js', () => ({ authConfigured: true }))
vi.mock('../../api.js', () => ({
  isWorkspaceBootstrapRequired: (error) => error?.status === 403
    && error?.body?.detail === 'verified subject has no active platform identity binding',
}))

const unbound = { status: 403, body: { detail: 'verified subject has no active platform identity binding' } }

function memoryStorage() {
  const values = new Map()
  return {
    getItem: vi.fn((key) => values.get(key) ?? null),
    setItem: vi.fn((key, value) => values.set(key, value)),
    removeItem: vi.fn((key) => values.delete(key)),
  }
}

// A new user: every projects read is unbound until the org exists.
function unboundServices(overrides = {}) {
  let created = null
  const services = {
    listProjects: vi.fn(async (orgId) => {
      if (!orgId && !created) throw unbound
      return []
    }),
    createOrg: vi.fn(async (name) => {
      created = { org_id: 'org-new', name }
      return created
    }),
    createProject: vi.fn(),
    openProject: vi.fn(),
    resolveOrgId: vi.fn(async () => null),
    ...overrides,
  }
  return services
}

function render(services, { enabled = true, onProvisioned = vi.fn(), wrapper } = {}) {
  const storage = memoryStorage()
  const hook = renderHook(() => useWorkspaceController({
    services, storage, formatError: (error) => error?.body?.detail || String(error?.message || error),
    autoProvision: { enabled, onProvisioned },
  }), wrapper ? { wrapper } : undefined)
  return { ...hook, storage, onProvisioned }
}

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

it('creates the first workspace once for an unbound signed-in user and asks the host to refresh', async () => {
  const services = unboundServices()
  const { result, onProvisioned } = render(services)
  await waitFor(() => expect(result.current.orgId).toBe('org-new'))
  await waitFor(() => expect(result.current.bootstrapState).toBe('bound'))
  expect(services.createOrg).toHaveBeenCalledTimes(1)
  expect(services.createOrg).toHaveBeenCalledWith(DEFAULT_WORKSPACE_NAME)
  expect(result.current.autoProvision).toBe('done')
  expect(onProvisioned).toHaveBeenCalledTimes(1)
  expect(onProvisioned).toHaveBeenCalledWith(expect.objectContaining({ org_id: 'org-new' }))
  expect(services.listProjects).toHaveBeenLastCalledWith('org-new')
})

it('shows the setting-up state while the create is in flight', async () => {
  let release
  const services = unboundServices({
    createOrg: vi.fn(() => new Promise((resolve) => { release = () => resolve({ org_id: 'org-new' }) })),
  })
  const { result } = render(services)
  await waitFor(() => expect(result.current.autoProvision).toBe('creating'))
  await act(async () => release())
  await waitFor(() => expect(result.current.autoProvision).toBe('done'))
  expect(services.createOrg).toHaveBeenCalledTimes(1)
})

it('never creates when the user already has an org', async () => {
  const services = unboundServices({ listProjects: vi.fn().mockResolvedValue([]) })
  const { result, onProvisioned } = render(services)
  await waitFor(() => expect(result.current.bootstrapState).toBe('bound'))
  expect(services.createOrg).not.toHaveBeenCalled()
  expect(result.current.autoProvision).toBe('idle')
  expect(onProvisioned).not.toHaveBeenCalled()
})

it('does nothing while auto-provision is disabled (signed out, guest, mock)', async () => {
  const services = unboundServices()
  const { result } = render(services, { enabled: false })
  await waitFor(() => expect(result.current.bootstrapState).toBe('unbound'))
  expect(services.createOrg).not.toHaveBeenCalled()
})

it('adopts the existing org when the create answers 409', async () => {
  const conflict = { status: 409, body: { detail: 'This identity is already assigned to a workspace.' } }
  const services = unboundServices({
    createOrg: vi.fn().mockRejectedValue(conflict),
    resolveOrgId: vi.fn().mockResolvedValue('org-existing'),
  })
  services.listProjects.mockImplementation(async (orgId) => {
    if (!orgId) throw unbound
    return []
  })
  const { result, onProvisioned } = render(services)
  await waitFor(() => expect(result.current.orgId).toBe('org-existing'))
  expect(services.createOrg).toHaveBeenCalledTimes(1)
  expect(services.resolveOrgId).toHaveBeenCalledTimes(1)
  expect(result.current.autoProvision).toBe('done')
  expect(onProvisioned).toHaveBeenCalledWith(expect.objectContaining({ org_id: 'org-existing', adopted: true }))
})

it('shows the 409 reason when no existing org can be adopted', async () => {
  const conflict = { status: 409, body: { detail: 'Contact your workspace administrator.' } }
  const services = unboundServices({ createOrg: vi.fn().mockRejectedValue(conflict) })
  const { result } = render(services)
  await waitFor(() => expect(result.current.autoProvision).toBe('failed'))
  expect(result.current.autoProvisionError).toBe('Contact your workspace administrator.')
  expect(services.createOrg).toHaveBeenCalledTimes(1)
})

it('explains a refusal with Retry, does not loop, and retry succeeds once allowed', async () => {
  const services = unboundServices()
  const created = services.createOrg.getMockImplementation()
  services.createOrg.mockRejectedValueOnce({ status: 403, body: { detail: 'invite_only' } })
  const { result, onProvisioned } = render(services)
  await waitFor(() => expect(result.current.autoProvision).toBe('failed'))
  expect(result.current.autoProvisionError).toMatch(/not available for this account/)
  // A refusal stays failed: no automatic second POST.
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(services.createOrg).toHaveBeenCalledTimes(1)
  services.createOrg.mockImplementation(created)
  await act(async () => { await result.current.retryAutoProvision() })
  await waitFor(() => expect(result.current.orgId).toBe('org-new'))
  expect(services.createOrg).toHaveBeenCalledTimes(2)
  expect(result.current.autoProvision).toBe('done')
  expect(onProvisioned).toHaveBeenCalledTimes(1)
})

it('explains a server error with Retry', async () => {
  const services = unboundServices({ createOrg: vi.fn().mockRejectedValue({ status: 503, body: { detail: 'down' } }) })
  const { result } = render(services)
  await waitFor(() => expect(result.current.autoProvision).toBe('failed'))
  expect(result.current.autoProvisionError).toBe('We could not set up your workspace. Please retry.')
})

it('StrictMode double mounting sends one create', async () => {
  const services = unboundServices()
  const { result } = render(services, { wrapper: StrictMode })
  await waitFor(() => expect(result.current.orgId).toBe('org-new'))
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(services.createOrg).toHaveBeenCalledTimes(1)
})

it('two mounted controllers share one in-flight create', async () => {
  let release
  const createOrg = vi.fn(() => new Promise((resolve) => { release = () => resolve({ org_id: 'org-new' }) }))
  const a = createWorkspaceController({ authLive: true, services: unboundServices({ createOrg }), storage: memoryStorage(),
    isBootstrapRequired: () => true })
  const b = createWorkspaceController({ authLive: true, services: unboundServices({ createOrg }), storage: memoryStorage(),
    isBootstrapRequired: () => true })
  await a.loadProjects(); await b.loadProjects()
  expect(a.getSnapshot().bootstrapState).toBe('unbound')
  const first = a.autoProvisionOrg()
  const second = b.autoProvisionOrg()
  expect(a.autoProvisionOrg()).toBe(first) // same controller: same promise
  await waitFor(() => expect(createOrg).toHaveBeenCalled())
  release()
  await Promise.all([first, second])
  expect(createOrg).toHaveBeenCalledTimes(1)
  expect(a.getSnapshot().orgId).toBe('org-new')
  expect(b.getSnapshot().orgId).toBe('org-new')
})
