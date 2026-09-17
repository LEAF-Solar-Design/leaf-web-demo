import { describe, expect, it, vi } from 'vitest'
import { createWorkspaceController, WORKSPACE_ORG_KEY } from './createWorkspaceController.js'

function setup(options = {}) {
  const storage = { getItem: vi.fn(() => 'stored-org'), setItem: vi.fn(), removeItem: vi.fn() }
  const services = {
    listProjects: vi.fn().mockResolvedValue([{ project_id: 'p1', name: 'Maple' }]),
    createOrg: vi.fn().mockResolvedValue({ org: { org_id: 'o1' } }),
    createProject: vi.fn().mockResolvedValue({ project_id: 'p1', name: 'Maple' }),
    openProject: vi.fn().mockResolvedValue({ project: { project_id: 'p1' } }),
  }
  const controller = createWorkspaceController({ services, storage, ...options })
  return { controller, services, storage }
}

describe('server workspace bootstrap', () => {
  it('lists projects without a stored org in live mode', async () => {
    const { controller, services, storage } = setup()
    expect(controller.getSnapshot().bootstrapState).toBe('unknown')
    await controller.loadProjects()
    expect(services.listProjects).toHaveBeenCalledWith(null)
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'bound', projectsLoaded: true, orgId: null })
    expect(controller.getSnapshot().projects).toHaveLength(1)
    expect(storage.getItem).not.toHaveBeenCalled()
  })

  it('recognizes an unbound verified identity', async () => {
    const { controller, services } = setup()
    services.listProjects.mockRejectedValue({ status: 403, body: { detail: 'verified subject has no active platform identity binding' } })
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unbound', projects: [], projectsLoaded: false })
  })

  it('explains other service failures', async () => {
    const formatError = vi.fn(() => 'Project storage is unavailable.')
    const { controller, services } = setup({ formatError })
    const error = { status: 500, body: { detail: 'raw service failure' } }
    services.listProjects.mockRejectedValue(error)
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unavailable', projectsError: 'Project storage is unavailable.' })
    expect(formatError).toHaveBeenCalledWith(error)
  })

  it.each([
    { detail: 'verified subject has no active platform tenant authority' },
    { error: { message: 'verified subject has no active platform identity binding' } },
    { error: { message: 'verified subject has no active platform tenant authority' } },
    { detail: 'access denied', error: { message: 'verified subject has no active platform tenant authority' } },
  ])('recognizes bootstrap details independently of formatting: %j', async (body) => {
    const { controller, services } = setup({ formatError: () => 'A human sentence.' })
    services.listProjects.mockRejectedValue({ status: 403, body })
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unbound', projects: [], projectsLoaded: false })
  })

  it.each([
    { status: 403, body: { detail: 'access denied' } },
    { status: 403, body: { detail: 'verified subject has no active platform identity binding elsewhere' } },
    { status: 500, body: { detail: 'verified subject has no active platform identity binding' } },
  ])('does not treat unrelated failures as unbound: %j', async (error) => {
    const { controller, services } = setup({ formatError: () => 'Please try again.' })
    services.listProjects.mockRejectedValue(error)
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unavailable', projectsError: 'Please try again.' })
  })

  it.each([true, false])('consults the injected bootstrap predicate: %s', async (required) => {
    const isBootstrapRequired = vi.fn(() => required)
    const { controller, services } = setup({ isBootstrapRequired })
    const error = { status: 403, body: { detail: 'verified subject has no active platform identity binding' } }
    services.listProjects.mockRejectedValue(error)
    await controller.loadProjects()
    expect(isBootstrapRequired).toHaveBeenCalledTimes(1)
    expect(isBootstrapRequired).toHaveBeenCalledWith(error)
    expect(controller.getSnapshot().bootstrapState).toBe(required ? 'unbound' : 'unavailable')
  })

  it('never reads or writes org storage during live creation', async () => {
    const { controller, storage } = setup()
    await controller.createOrg('My workspace')
    await controller.loadProjects()
    expect(storage.getItem).not.toHaveBeenCalled()
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(storage.removeItem).not.toHaveBeenCalled()
  })

  it('preserves the auth-off org header seam', async () => {
    const { controller, services, storage } = setup({ authLive: false })
    await controller.loadProjects()
    expect(storage.getItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY)
    expect(services.listProjects).toHaveBeenCalledWith('stored-org')
    await controller.createOrg('My workspace')
    expect(storage.setItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY, 'o1')
  })

  it('retains the org error without adopting an org on failure', async () => {
    const { controller, services } = setup()
    services.createOrg.mockRejectedValue(new Error('Workspace creation failed.'))
    expect(await controller.createOrg('Draft workspace')).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ orgId: null, orgDraftError: 'Workspace creation failed.', orgBusy: false })
  })

  it('surfaces the conflict verbatim and recovers through a list read', async () => {
    const { controller, services } = setup({ formatError: () => 'A formatted conflict.' })
    const sentence = 'This identity is already bound to a differently named workspace.'
    services.createOrg.mockRejectedValue({ status: 409, body: { detail: sentence } })
    await controller.createOrg('Retained name')
    expect(controller.getSnapshot()).toMatchObject({ orgDraftError: sentence, orgConflict: true })
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'bound', orgDraftError: null, orgConflict: false })
    expect(services.createOrg).toHaveBeenCalledTimes(1)
  })

  it('publishes the failed project draft error', async () => {
    const formatError = vi.fn(() => 'Project creation failed.')
    const { controller, services } = setup({ formatError })
    const error = { status: 500, body: { detail: 'raw project failure' } }
    services.createProject.mockRejectedValue(error)
    expect(await controller.createProject('Retained project')).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ projectDraftError: 'Project creation failed.', projectBusy: false })
    expect(formatError).toHaveBeenCalledWith(error)
  })

  it('formats non-conflict workspace creation failures', async () => {
    const formatError = vi.fn(() => 'Workspace creation is unavailable. Please retry.')
    const { controller, services } = setup({ formatError })
    const error = { status: 500, body: { detail: 'raw workspace failure' } }
    services.createOrg.mockRejectedValue(error)
    await controller.createOrg('Retained workspace')
    expect(controller.getSnapshot()).toMatchObject({
      orgId: null, orgDraftError: 'Workspace creation is unavailable. Please retry.', orgConflict: false, orgBusy: false,
    })
    expect(formatError).toHaveBeenCalledWith(error)
  })

  it.each([
    [{ error: { message: 'This identity already has a workspace.' } }, 'This identity already has a workspace.'],
    [{}, 'A formatted conflict.'],
  ])('uses the conflict envelope or explained fallback: %j', async (body, sentence) => {
    const { controller, services } = setup({ formatError: () => 'A formatted conflict.' })
    services.createOrg.mockRejectedValue({ status: 409, body })
    await controller.createOrg('Retained workspace')
    expect(controller.getSnapshot()).toMatchObject({ orgDraftError: sentence, orgConflict: true })
  })
})
