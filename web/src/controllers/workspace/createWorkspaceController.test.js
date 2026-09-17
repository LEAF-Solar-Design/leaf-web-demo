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
  it.each([undefined, false])('refuses a list without an org when authLive is %s', async (authLive) => {
    const storage = { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() }
    const { controller, services } = setup({ storage, ...(authLive === undefined ? {} : { authLive }) })
    expect(await controller.loadProjects()).toEqual([])
    expect(services.listProjects).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unbound', projectsLoading: false, projectsLoaded: false })
  })

  it('reads only projects from a list envelope while a workspace opens', async () => {
    const { controller, services } = setup({ authLive: true })
    let resolveOpen
    services.openProject.mockImplementation(() => new Promise((resolve) => { resolveOpen = resolve }))
    const opening = controller.openProject('p1')
    services.listProjects.mockResolvedValue({ org_id: 'different-org', projects: [{ project_id: 'p1', name: 'Maple' }] })
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ orgId: 'stored-org', bootstrapState: 'bound', workspaceLoading: true })
    const workspace = { project: { project_id: 'p1' } }
    resolveOpen(workspace)
    expect(await opening).toEqual(workspace)
    expect(controller.getSnapshot()).toMatchObject({ workspace, workspaceLoading: false })
  })

  it('keeps the adopted org when persistence fails and mock mode changes', () => {
    const storage = {
      getItem: vi.fn(() => 'o1'),
      setItem: vi.fn(() => { throw new Error('Storage unavailable') }),
      removeItem: vi.fn(),
    }
    const { controller } = setup({ authLive: false, storage })
    expect(controller.adoptOrgId('o2')).toBe(true)
    controller.setMock(true)
    expect(controller.getSnapshot().orgId).toBe('o2')
    expect(storage.getItem).toHaveBeenCalledTimes(1)
    expect(storage.setItem).toHaveBeenCalledExactlyOnceWith(WORKSPACE_ORG_KEY, 'o2')
    expect(storage.removeItem).not.toHaveBeenCalled()
  })

  it('persists an adopted org in live mode', () => {
    const { controller, storage } = setup({ authLive: true })
    controller.adoptOrgId('o2')
    expect(controller.getSnapshot().orgId).toBe('o2')
    expect(storage.setItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY, 'o2')
  })

  it('allows only Alpha to create a workspace while its request is pending', async () => {
    const { controller, services } = setup({ authLive: true })
    let resolveOrg
    services.createOrg.mockImplementationOnce(() => new Promise((resolve) => { resolveOrg = resolve }))
      .mockRejectedValue({ status: 409, body: { detail: 'Already bound to Alpha.' } })
    const alpha = controller.createOrg('Alpha')
    expect(await controller.createOrg('Beta')).toBeNull()
    expect(services.createOrg).toHaveBeenCalledExactlyOnceWith('Alpha')
    resolveOrg({ org: { org_id: 'alpha' } })
    await alpha
    expect(controller.getSnapshot()).toMatchObject({ orgId: 'alpha', bootstrapState: 'bound', orgBusy: false, orgConflict: false, orgDraftError: null, projectsError: null })
  })

  it('keeps project creation single-flight across listing and hydration', async () => {
    const { controller, services } = setup({ authLive: true })
    let resolveProject
    let resolveOpen
    services.createProject.mockImplementationOnce(() => new Promise((resolve) => { resolveProject = resolve }))
    services.openProject.mockImplementationOnce(() => new Promise((resolve) => { resolveOpen = resolve }))
    const creating = controller.createProject('Alpha')
    expect(await controller.createProject('Beta')).toBeNull()
    services.listProjects.mockResolvedValue({ org_id: 'different-org', projects: [] })
    await controller.loadProjects()
    resolveProject({ project_id: 'p2', name: 'Alpha' })
    await Promise.resolve()
    expect(await controller.createProject('Gamma')).toBeNull()
    expect(services.createProject).toHaveBeenCalledExactlyOnceWith('Alpha', 'stored-org')
    resolveOpen({ project: { project_id: 'p2' } })
    await creating
    expect(controller.getSnapshot()).toMatchObject({ orgId: 'stored-org', projectBusy: false, workspaceLoading: false })
  })

  it('lists projects without a stored org in live mode', async () => {
    const storage = { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() }
    const { controller, services } = setup({ authLive: true, storage })
    expect(controller.getSnapshot().bootstrapState).toBe('unknown')
    await controller.loadProjects()
    expect(services.listProjects).toHaveBeenCalledWith(null)
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'bound', projectsLoaded: true, orgId: null })
    expect(controller.getSnapshot().projects).toHaveLength(1)
    expect(storage.getItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY)
  })

  it('recognizes an unbound verified identity', async () => {
    const { controller, services, storage } = setup({ authLive: true })
    services.listProjects.mockRejectedValue({ status: 403, body: { detail: 'verified subject has no active platform identity binding' } })
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unbound', orgId: null, projects: [], projectsLoaded: false })
    expect(storage.removeItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY)
  })

  it('explains other service failures', async () => {
    const formatError = vi.fn(() => 'Project storage is unavailable.')
    const { controller, services, storage } = setup({ authLive: true, formatError })
    const error = { status: 500, body: { detail: 'raw service failure' } }
    services.listProjects.mockRejectedValue(error)
    await controller.loadProjects()
    expect(controller.getSnapshot()).toMatchObject({ bootstrapState: 'unavailable', projectsError: 'Project storage is unavailable.' })
    expect(formatError).toHaveBeenCalledWith(error)
    expect(controller.getSnapshot().orgId).toBe('stored-org')
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(storage.removeItem).not.toHaveBeenCalled()
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

  it('reads and writes the org cache during live creation', async () => {
    const { controller, storage } = setup({ authLive: true })
    await controller.createOrg('My workspace')
    await controller.loadProjects()
    expect(storage.getItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY)
    expect(storage.setItem).toHaveBeenCalledWith(WORKSPACE_ORG_KEY, 'o1')
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
    expect(controller.getSnapshot()).toMatchObject({ orgId: 'stored-org', orgDraftError: 'Workspace creation failed.', projectsError: 'Workspace creation failed.', orgBusy: false })
    services.createOrg.mockResolvedValue({ org: { org_id: 'o2' } })
    await controller.createOrg('Draft workspace')
    expect(controller.getSnapshot()).toMatchObject({ orgId: 'o2', orgDraftError: null, projectsError: null, orgConflict: false })
  })

  it('surfaces the conflict verbatim and recovers through a list read', async () => {
    const { controller, services } = setup({ formatError: () => 'A formatted conflict.' })
    const sentence = 'This identity is already bound to a differently named workspace.'
    services.createOrg.mockRejectedValue({ status: 409, body: { detail: sentence } })
    await controller.createOrg('Retained name')
    expect(controller.getSnapshot()).toMatchObject({ orgDraftError: sentence, projectsError: sentence, orgConflict: true })
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
    expect(controller.getSnapshot()).toMatchObject({ projectDraftError: 'Project creation failed.', projectsError: 'Project creation failed.', projectBusy: false })
    expect(formatError).toHaveBeenCalledWith(error)
    services.createProject.mockResolvedValue({ project_id: 'p2', name: 'Retained project' })
    await controller.createProject('Retained project')
    expect(controller.getSnapshot()).toMatchObject({ projectDraftError: null, projectsError: null, projectBusy: false })
  })

  it('formats non-conflict workspace creation failures', async () => {
    const formatError = vi.fn(() => 'Workspace creation is unavailable. Please retry.')
    const { controller, services } = setup({ formatError })
    const error = { status: 500, body: { detail: 'raw workspace failure' } }
    services.createOrg.mockRejectedValue(error)
    await controller.createOrg('Retained workspace')
    expect(controller.getSnapshot()).toMatchObject({
      orgId: 'stored-org', orgDraftError: 'Workspace creation is unavailable. Please retry.', orgConflict: false, orgBusy: false,
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
