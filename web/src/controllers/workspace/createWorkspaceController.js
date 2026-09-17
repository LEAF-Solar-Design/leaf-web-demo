export const WORKSPACE_ORG_KEY = 'leaf.org_id'

function browserStorage() {
  try { return globalThis.localStorage } catch { return null }
}

export function readStoredOrgId(storage = browserStorage()) {
  try { return storage?.getItem(WORKSPACE_ORG_KEY) || null } catch { return null }
}

export function writeStoredOrgId(id, storage = browserStorage()) {
  try {
    if (id) storage?.setItem(WORKSPACE_ORG_KEY, id)
    else storage?.removeItem(WORKSPACE_ORG_KEY)
  } catch { /* workspace persistence is best effort */ }
}

const defaultFormatError = (error) => String(error?.message || error)

function defaultIsBootstrapRequired(error) {
  const details = [
    'verified subject has no active platform identity binding',
    'verified subject has no active platform tenant authority',
  ]
  return error?.status === 403 && (
    details.includes(error?.body?.detail) || details.includes(error?.body?.error?.message)
  )
}

function invoke(callback, value) {
  try { callback?.(value) } catch { /* presentation callbacks do not own controller state */ }
}

function projectIdOf(project) {
  return project?.project_id || project?.id || null
}

function orgIdOf(org) {
  return org?.org_id || org?.id || null
}

/**
 * Framework-neutral owner for the org/project workspace lifecycle.
 *
 * Services are injected so this controller can be mounted by either surface:
 * createOrg(name), listProjects(orgId), createProject(name, orgId), and
 * openProject(projectId, orgId). Storage is also injected for deterministic
 * tests. The controller never prompts and never imports presentation code.
 */
export function createWorkspaceController({
  mock = false,
  authLive = true,
  services,
  storage = browserStorage(),
  formatError = defaultFormatError,
  isBootstrapRequired = defaultIsBootstrapRequired,
  callbacks = {},
} = {}) {
  if (!services) throw new TypeError('createWorkspaceController requires services')

  const listeners = new Set()
  let disposed = false
  const generations = { projects: 0, workspace: 0, org: 0, project: 0 }
  let state = {
    mock: !!mock,
    orgId: mock || !authLive ? readStoredOrgId(storage) : null,
    bootstrapState: 'unknown',
    projectsLoaded: false,
    orgDraftError: null,
    orgConflict: false,
    projectDraftError: null,
    projects: [],
    projectsError: null,
    projectsLoading: false,
    openProjectId: null,
    workspace: null,
    canonicalVersionId: null,
    workspaceLoading: false,
    orgBusy: false,
    projectBusy: false,
  }

  const getSnapshot = () => state
  const subscribe = (listener) => {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }
  const publish = (patch) => {
    if (disposed) return
    state = { ...state, ...patch }
    listeners.forEach((listener) => listener())
  }
  const nextGeneration = (lane) => ++generations[lane]
  const isCurrent = (lane, request) => !disposed && generations[lane] === request
  const invalidateAll = () => Object.keys(generations).forEach((lane) => { generations[lane] += 1 })
  const explain = (error) => {
    try { return formatError(error) } catch { return defaultFormatError(error) }
  }

  async function loadProjects() {
    const request = nextGeneration('projects')
    const orgId = state.orgId
    if (state.mock || (!authLive && !orgId)) {
      publish({ projects: [], projectsError: null, projectsLoading: false, projectsLoaded: false, bootstrapState: orgId ? 'bound' : 'unbound' })
      return []
    }
    publish({ projectsLoading: true, projectsError: null })
    try {
      const response = await services.listProjects(orgId)
      if (!isCurrent('projects', request) || state.orgId !== orgId) return null
      const projects = Array.isArray(response) ? response : response?.projects || []
      const responseOrgId = response?.org_id || orgIdOf(response?.org)
      publish({ projects, projectsLoading: false, projectsLoaded: true, bootstrapState: 'bound',
        ...(responseOrgId ? { orgId: responseOrgId } : {}), orgConflict: false, orgDraftError: null })
      return projects || []
    } catch (error) {
      if (!isCurrent('projects', request) || state.orgId !== orgId) return null
      const message = explain(error)
      const unbound = isBootstrapRequired(error)
      publish({ projects: [], projectsError: message, projectsLoading: false, projectsLoaded: false,
        bootstrapState: unbound ? 'unbound' : 'unavailable' })
      return null
    }
  }

  async function openProject(projectId) {
    if (!projectId) return null
    const request = nextGeneration('workspace')
    const orgId = state.orgId
    publish({
      openProjectId: projectId,
      workspace: null,
      canonicalVersionId: null,
      workspaceLoading: true,
      projectsError: null,
    })
    try {
      const workspace = await services.openProject(projectId, orgId)
      if (!isCurrent('workspace', request) || state.openProjectId !== projectId || state.orgId !== orgId) return null
      publish({ workspace, workspaceLoading: false })
      invoke(callbacks.onWorkspaceOpened, { projectId, orgId, workspace })
      return workspace
    } catch (error) {
      if (!isCurrent('workspace', request) || state.openProjectId !== projectId || state.orgId !== orgId) return null
      publish({ workspace: null, projectsError: explain(error), workspaceLoading: false })
      return null
    }
  }

  async function rehydrate() {
    const projectId = state.openProjectId
    if (!projectId) return null
    const request = nextGeneration('workspace')
    const orgId = state.orgId
    publish({ workspaceLoading: true })
    try {
      const workspace = await services.openProject(projectId, orgId)
      if (!isCurrent('workspace', request) || state.openProjectId !== projectId || state.orgId !== orgId) return null
      publish({ workspace, workspaceLoading: false })
      invoke(callbacks.onWorkspaceRehydrated, { projectId, orgId, workspace })
      return workspace
    } catch {
      if (!isCurrent('workspace', request) || state.openProjectId !== projectId || state.orgId !== orgId) return null
      // A transient refresh failure must retain the last good hydration.
      publish({ workspaceLoading: false })
      return null
    }
  }

  function closeProject() {
    nextGeneration('workspace')
    const projectId = state.openProjectId
    publish({
      openProjectId: null,
      workspace: null,
      canonicalVersionId: null,
      workspaceLoading: false,
    })
    invoke(callbacks.onWorkspaceClosed, { projectId })
  }

  function adoptOrgId(givenOrgId) {
    const orgId = String(givenOrgId || '').trim()
    if (!orgId) return false
    if (state.orgId === orgId) return true
    invalidateAll()
    if (state.mock || !authLive) writeStoredOrgId(orgId, storage)
    publish({
      orgId,
      bootstrapState: 'unknown',
      projectsLoaded: false,
      projects: [],
      projectsError: null,
      projectsLoading: false,
      openProjectId: null,
      workspace: null,
      canonicalVersionId: null,
      workspaceLoading: false,
      orgBusy: false,
      projectBusy: false,
    })
    return true
  }

  async function createOrg(givenName) {
    if (givenName == null) return null
    const name = String(givenName).trim() || 'My workspace'
    const request = nextGeneration('org')
    publish({ orgBusy: true, projectsError: null, orgDraftError: null, orgConflict: false })
    try {
      const response = await services.createOrg(name)
      const org = response?.org || response
      const orgId = orgIdOf(org)
      if (!orgId) throw new Error('The workspace service returned an org without an id.')
      if (!isCurrent('org', request)) return null
      generations.projects += 1
      generations.workspace += 1
      generations.project += 1
      // Preserve App ordering: persistence first, then the in-memory org and list.
      if (state.mock || !authLive) writeStoredOrgId(orgId, storage)
      publish({
        orgId,
        bootstrapState: 'bound',
        projectsLoaded: false,
        projects: [],
        projectsLoading: false,
        workspaceLoading: false,
        orgBusy: false,
        projectBusy: false,
      })
      invoke(callbacks.onOrgCreated, org)
      return org
    } catch (error) {
      if (!isCurrent('org', request)) return null
      const orgConflict = error?.status === 409
      const message = orgConflict
        ? error?.body?.detail || error?.body?.error?.message || explain(error)
        : explain(error)
      publish({ orgDraftError: message, orgConflict, orgBusy: false })
      return null
    }
  }

  async function createProject(givenName) {
    if (givenName == null || !String(givenName).trim()) return null
    const name = String(givenName).trim()
    const request = nextGeneration('project')
    const orgId = state.orgId
    publish({ projectBusy: true, projectsError: null, projectDraftError: null })
    try {
      const project = await services.createProject(name, orgId)
      const projectId = projectIdOf(project)
      if (!projectId) throw new Error('The workspace service returned a project without an id.')
      if (!isCurrent('project', request) || state.orgId !== orgId) return null
      // The create endpoint is idempotent, so reconcile a replay by identity.
      const nextProjects = state.projects.some((item) => projectIdOf(item) === projectId)
        ? state.projects.map((item) => projectIdOf(item) === projectId ? project : item)
        : [...state.projects, project]
      publish({ projects: nextProjects })
      invoke(callbacks.onProjectCreated, project)

      const openRequest = nextGeneration('workspace')
      publish({
        openProjectId: projectId,
        workspace: null,
        canonicalVersionId: null,
        workspaceLoading: true,
        projectsError: null,
      })
      try {
        const workspace = await services.openProject(projectId, orgId)
        if (!isCurrent('workspace', openRequest) || state.openProjectId !== projectId || state.orgId !== orgId) return project
        publish({ workspace, workspaceLoading: false })
        invoke(callbacks.onWorkspaceOpened, { projectId, orgId, workspace })
      } catch (error) {
        if (isCurrent('workspace', openRequest) && state.openProjectId === projectId && state.orgId === orgId) {
          publish({ workspace: null, projectsError: explain(error), workspaceLoading: false })
        }
      } finally {
        if (isCurrent('project', request)) publish({ projectBusy: false })
      }
      return project
    } catch (error) {
      if (!isCurrent('project', request)) return null
      publish({ projectDraftError: explain(error), projectBusy: false })
      return null
    }
  }

  function selectCanonicalVersion(versionId) {
    publish({ canonicalVersionId: versionId || null })
    invoke(callbacks.onCanonicalVersionSelected, versionId || null)
  }

  function setMock(nextMock) {
    const value = !!nextMock
    if (state.mock === value) return
    invalidateAll()
    publish({
      mock: value,
      orgId: value || !authLive ? readStoredOrgId(storage) : null,
      bootstrapState: 'unknown',
      projectsLoaded: false,
      orgDraftError: null,
      orgConflict: false,
      projectDraftError: null,
      projects: [],
      projectsError: null,
      projectsLoading: false,
      openProjectId: null,
      workspace: null,
      canonicalVersionId: null,
      workspaceLoading: false,
      orgBusy: false,
      projectBusy: false,
    })
  }

  function dispose() {
    disposed = true
    invalidateAll()
    listeners.clear()
  }

  function start() {
    disposed = false
  }

  return {
    getSnapshot,
    subscribe,
    loadProjects,
    openProject,
    rehydrate,
    closeProject,
    adoptOrgId,
    createOrg,
    createProject,
    selectCanonicalVersion,
    setMock,
    start,
    dispose,
  }
}

export function selectCurrentProjectName(state) {
  if (!state?.openProjectId) return null
  return state.workspace?.project?.name
    || state.projects?.find((project) => projectIdOf(project) === state.openProjectId)?.name
    || null
}

export function selectCanonicalVersion(state) {
  if (!state?.canonicalVersionId) return null
  return state.workspace?.drawing_versions?.find(
    (version) => version.version_id === state.canonicalVersionId,
  ) || null
}
