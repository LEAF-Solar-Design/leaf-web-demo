import { humanizeError } from '../../errorHumanize.js'
import { authorityStorage, authorityKey, serviceState } from '../../lib/serviceAuthSession.js'

export const WORKSPACE_ORG_KEY = 'leaf.org_id'
// A stable default keeps POST /api/orgs idempotent: the server returns the
// caller's existing org only when the retried name matches.
export const DEFAULT_WORKSPACE_NAME = 'My workspace'

// One first-sign-in workspace POST per authority and name, shared across
// controller instances, so a remount or a second mount joins the request in
// flight instead of sending another. Entries leave the map when they settle.
const autoProvisionFlights = new Map()
function sharedCreateOrg(services, name) {
  const key = `${authorityKey() || 'anonymous'}
${name}`
  let flight = autoProvisionFlights.get(key)
  if (!flight) {
    flight = Promise.resolve()
      .then(() => services.createOrg(name))
      .finally(() => autoProvisionFlights.delete(key))
    autoProvisionFlights.set(key, flight)
  }
  return flight
}

const AUTO_PROVISION_REFUSED = 'Self-serve workspaces are not available for this account. '
  + 'Ask your workspace administrator for an invitation, then sign in again.'
const AUTO_PROVISION_FAILED = 'We could not set up your workspace. Please retry.'
const AUTO_PROVISION_MAX_SUPERSEDED = 2

const projectStorageKey = (orgId) => `leaf.project_id:${orgId}`

function readStoredProjectId(orgId, storage) {
  if (!orgId) return null
  try { return storage?.getItem(projectStorageKey(orgId)) || null } catch { return null }
}

function writeStoredProjectId(orgId, projectId, storage) {
  if (!orgId) return
  try {
    if (projectId) storage?.setItem(projectStorageKey(orgId), projectId)
    else storage?.removeItem(projectStorageKey(orgId))
  } catch { /* workspace persistence is best effort */ }
}

function browserStorage() {
  // Resolve every operation against the current authority, including after login.
  return {
    getItem: key => key === WORKSPACE_ORG_KEY && serviceState().mode
      ? serviceState().identity?.org_id || null : authorityStorage()?.getItem(key),
    setItem: (key, value) => authorityStorage()?.setItem(key, value),
    removeItem: key => authorityStorage()?.removeItem(key),
  }
}

/** Make the next /app boot resume this project for this org (M3 S4 opens a sent copy there). */
export function rememberOpenProject(orgId, projectId, storage = browserStorage()) {
  writeStoredProjectId(orgId, projectId, storage)
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
  authLive = false,
  services,
  storage,
  formatError = defaultFormatError,
  isBootstrapRequired = defaultIsBootstrapRequired,
  callbacks = {},
} = {}) {
  if (!services) throw new TypeError('createWorkspaceController requires services')
  const followsAuthority = storage === undefined
  storage = storage === undefined ? browserStorage() : storage

  const listeners = new Set()
  let disposed = false
  let readsPaused = false // a known guest: loadProjects publishes the empty, error-free snapshot and reads nothing
  const generations = { projects: 0, workspace: 0, org: 0, project: 0 }
  const authorities = {}
  let inFlightOpen = null
  let resumeAttempted = false
  let state = {
    mock: !!mock,
    orgId: readStoredOrgId(storage),
    bootstrapState: 'unknown',
    bootstrapMessage: null,
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
    workspaceError: null,
    orgBusy: false,
    projectBusy: false,
    orgErrorStatus: null,
    autoProvision: 'idle', // idle | creating | done | failed
    autoProvisionError: null,
  }
  let autoProvisionPromise = null
  let autoProvisionSuperseded = 0

  const getSnapshot = () => state
  const subscribe = (listener) => {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }
  const publish = (patch) => {
    if (disposed) return
    if (patch.workspaceLoading === true || patch.workspace || patch.openProjectId === null) {
      patch.workspaceError = null
    }
    state = { ...state, ...patch }
    listeners.forEach((listener) => listener())
  }
  const nextGeneration = (lane) => {
    if (lane === 'workspace') inFlightOpen = null
    authorities[lane] = authorityKey()
    return ++generations[lane]
  }
  const isCurrent = (lane, request) => !disposed && generations[lane] === request
    && (!followsAuthority || authorities[lane] === authorityKey())
  const invalidateAll = () => Object.keys(generations).forEach((lane) => { nextGeneration(lane) })
  const explain = (error) => {
    try { return formatError(error) } catch { return defaultFormatError(error) }
  }
  // Only null-to-org adoption preserves the workspace generation.
  const isCurrentWorkspace = (request, projectId, orgId) => isCurrent('workspace', request)
    && state.openProjectId === projectId && (state.orgId === orgId || orgId === null)
  const acceptWorkspaceOrg = (workspace, orgId) => {
    if (orgId !== null || state.orgId === null) return true
    const mismatched = [workspace?.org_id, workspace?.project?.org_id].some(
      (id) => typeof id === 'string' && id.length > 0 && id !== state.orgId,
    )
    if (!mismatched) return true
    publish({ openProjectId: null, workspace: null, canonicalVersionId: null, workspaceLoading: false })
    return false
  }

  async function loadProjects() {
    const request = nextGeneration('projects')
    const orgId = state.orgId
    const workspaceRequest = generations.workspace
    if (state.mock || readsPaused || (!authLive && !orgId)) {
      publish({ projects: [], projectsError: null, projectsLoading: false, projectsLoaded: false, bootstrapState: orgId ? 'bound' : 'unbound', bootstrapMessage: null })
      return []
    }
    publish({ projectsLoading: true, projectsError: null })
    try {
      const response = await services.listProjects(orgId)
      if (!isCurrent('projects', request) || state.orgId !== orgId) return null
      const projects = Array.isArray(response) ? response : response?.projects || []
      publish({ projects, projectsLoading: false, projectsLoaded: true, bootstrapState: 'bound',
        bootstrapMessage: null, orgConflict: false, orgDraftError: null })
      // Publication can synchronously trigger a manual selection or another list.
      if (isCurrent('projects', request) && state.orgId === orgId
        && generations.workspace === workspaceRequest && !state.mock
        && !state.openProjectId && !state.projectBusy && !resumeAttempted) {
        const projectId = readStoredProjectId(orgId, storage)
        if (projectId && projects.some((project) => projectIdOf(project) === projectId)) {
          resumeAttempted = true
          await openProject(projectId)
        }
      }
      return projects || []
    } catch (error) {
      if (!isCurrent('projects', request) || state.orgId !== orgId) return null
      const message = explain(error)
      const unbound = isBootstrapRequired(error)
      if (unbound) {
        invalidateAll()
        writeStoredOrgId(null, storage)
        publish({ projects: [], projectsError: null, projectsLoading: false, projectsLoaded: false,
          bootstrapState: 'unbound', bootstrapMessage: message, orgId: null,
          openProjectId: null, workspace: null, canonicalVersionId: null,
          workspaceLoading: false, projectBusy: false, orgBusy: false })
      } else {
        publish({ projects: [], projectsError: message, projectsLoading: false, projectsLoaded: false,
          bootstrapState: 'unavailable', bootstrapMessage: null })
      }
      return null
    }
  }

  function openProject(projectId) {
    if (!projectId) return Promise.resolve(null)
    resumeAttempted = true
    const request = nextGeneration('workspace')
    const orgId = state.orgId
    let resolveOpen
    let rejectOpen
    const promise = new Promise((resolve, reject) => { resolveOpen = resolve; rejectOpen = reject })
    const opening = { request, projectId, orgId, promise }
    inFlightOpen = opening
    // Register before publishing: a subscriber may synchronously request hydration.
    performOpen().then(
      (workspace) => {
        if (inFlightOpen === opening) inFlightOpen = null
        resolveOpen(workspace)
      },
      (error) => {
        if (inFlightOpen === opening) inFlightOpen = null
        rejectOpen(error)
      },
    )
    return promise

    async function performOpen() {
      publish({
        openProjectId: projectId,
        workspace: null,
        canonicalVersionId: null,
        workspaceLoading: true,
        projectsError: null,
      })
      try {
        const workspace = await services.openProject(projectId, orgId)
        if (!isCurrentWorkspace(request, projectId, orgId) || !acceptWorkspaceOrg(workspace, orgId)) return null
        const settledOrgId = state.orgId
        if (!state.mock) writeStoredProjectId(settledOrgId, projectId, storage)
        publish({ workspace, workspaceLoading: false })
        invoke(callbacks.onWorkspaceOpened, { projectId, orgId: settledOrgId, workspace })
        return workspace
      } catch {
        if (!isCurrentWorkspace(request, projectId, orgId)) return null
        publish({ workspace: null, workspaceError: humanizeError(null), workspaceLoading: false })
        return null
      }
    }
  }

  async function rehydrate() {
    const projectId = state.openProjectId
    if (!projectId) return null
    if (inFlightOpen && isCurrentWorkspace(inFlightOpen.request, inFlightOpen.projectId, inFlightOpen.orgId)) {
      return inFlightOpen.promise
    }
    const request = nextGeneration('workspace')
    const orgId = state.orgId
    publish({ workspaceLoading: true })
    try {
      const workspace = await services.openProject(projectId, orgId)
      if (!isCurrentWorkspace(request, projectId, orgId) || !acceptWorkspaceOrg(workspace, orgId)) return null
      const settledOrgId = state.orgId
      if (!state.mock) writeStoredProjectId(settledOrgId, projectId, storage)
      publish({ workspace, workspaceLoading: false })
      invoke(callbacks.onWorkspaceRehydrated, { projectId, orgId: settledOrgId, workspace })
      return workspace
    } catch {
      if (!isCurrentWorkspace(request, projectId, orgId)) return null
      // A transient refresh failure must retain the last good hydration.
      publish({ workspaceLoading: false, ...(!state.workspace ? { workspaceError: humanizeError(null) } : {}) })
      return null
    }
  }

  function closeProject() {
    nextGeneration('workspace')
    resumeAttempted = true
    if (!state.mock) writeStoredProjectId(state.orgId, null, storage)
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
    resumeAttempted = false
    const adopting = state.orgId === null
    if (adopting) {
      nextGeneration('projects')
      nextGeneration('org')
      nextGeneration('project')
    } else invalidateAll()
    writeStoredOrgId(orgId, storage)
    publish({
      orgId,
      bootstrapState: 'unknown',
      bootstrapMessage: null,
      projectsLoaded: false,
      projects: [],
      projectsError: null,
      projectsLoading: false,
      ...(!adopting && {
        openProjectId: null,
        workspace: null,
        canonicalVersionId: null,
        workspaceLoading: false,
      }),
      orgBusy: false,
      projectBusy: false,
    })
    return true
  }

  function createOrg(givenName) {
    return runCreateOrg(givenName, (name) => services.createOrg(name))
  }

  async function runCreateOrg(givenName, send) {
    if (state.orgBusy || givenName == null) return null
    const name = String(givenName).trim() || DEFAULT_WORKSPACE_NAME
    const request = nextGeneration('org')
    publish({ orgBusy: true, projectsError: null, orgDraftError: null, orgConflict: false, orgErrorStatus: null })
    try {
      const response = await send(name)
      const org = response?.org || response
      const orgId = orgIdOf(org)
      if (!orgId) throw new Error('The workspace service returned an org without an id.')
      if (!isCurrent('org', request)) return null
      generations.projects += 1
      nextGeneration('workspace')
      generations.project += 1
      resumeAttempted = false
      // Preserve App ordering: persistence first, then the in-memory org and list.
      writeStoredOrgId(orgId, storage)
      publish({
        orgId,
        bootstrapState: 'bound',
        bootstrapMessage: null,
        projectsError: null,
        orgDraftError: null,
        orgConflict: false,
        projectDraftError: null,
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
      publish({ orgDraftError: message, projectsError: message, orgConflict, orgBusy: false,
        orgErrorStatus: typeof error?.status === 'number' ? error.status : null })
      return null
    }
  }

  // A 409 means the server already holds a workspace for this identity (or
  // needs an administrator). Adopt the org the verified session resolves to.
  async function adoptExistingOrg() {
    if (typeof services.resolveOrgId !== 'function') return null
    try {
      const orgId = await services.resolveOrgId()
      if (disposed || !orgId || !adoptOrgId(orgId)) return null
      return { org_id: String(orgId).trim(), adopted: true }
    } catch {
      return null
    }
  }

  /**
   * First sign-in: create the caller's workspace once, without a prompt.
   *
   * Runs only for a live-auth caller whose projects read proved them unbound
   * (bootstrapState 'unbound', no org). One request per controller and one per
   * authority across controllers; a failure stays failed until retry is asked
   * for, so a refusal never loops. Resolves to the org, or null.
   */
  function autoProvisionOrg({ name = DEFAULT_WORKSPACE_NAME, retry = false } = {}) {
    if (autoProvisionPromise) return autoProvisionPromise
    if (disposed || !authLive || state.mock || readsPaused || state.orgId
      || state.bootstrapState !== 'unbound' || state.orgBusy) return Promise.resolve(null)
    if (state.autoProvision === 'done' || state.autoProvision === 'creating') return Promise.resolve(null)
    if (state.autoProvision === 'failed' && !retry) return Promise.resolve(null)
    if (retry) autoProvisionSuperseded = 0
    publish({ autoProvision: 'creating', autoProvisionError: null })
    const settle = async () => {
      const org = await runCreateOrg(name, (orgName) => sharedCreateOrg(services, orgName))
      if (disposed) return null
      if (org) return org
      if (state.orgConflict) {
        const adopted = await adoptExistingOrg()
        if (adopted) return adopted
      }
      return null
    }
    const promise = settle().then((org) => {
      if (disposed) return null
      if (org) {
        autoProvisionSuperseded = 0
        publish({ autoProvision: 'done', autoProvisionError: null })
        invoke(callbacks.onAutoProvisioned, org)
        return org
      }
      if (state.orgDraftError == null && ++autoProvisionSuperseded <= AUTO_PROVISION_MAX_SUPERSEDED) {
        // Superseded by an authority or generation change: allow one more try.
        publish({ autoProvision: 'idle' })
        return null
      }
      const status = state.orgErrorStatus
      const message = status === 403 ? AUTO_PROVISION_REFUSED
        : status === 409 && state.orgDraftError ? state.orgDraftError
          : AUTO_PROVISION_FAILED
      publish({ autoProvision: 'failed', autoProvisionError: message })
      return null
    }).finally(() => {
      if (autoProvisionPromise === promise) autoProvisionPromise = null
    })
    autoProvisionPromise = promise
    return promise
  }

  function retryAutoProvision(options = {}) {
    return autoProvisionOrg({ ...options, retry: true })
  }

  async function createProject(givenName, { requireCreated = false } = {}) {
    if (state.projectBusy || givenName == null || !String(givenName).trim()) return null
    const name = String(givenName).trim()
    const request = nextGeneration('project')
    const orgId = state.orgId
    const workspaceRequest = generations.workspace
    publish({ projectBusy: true, projectsError: null, projectDraftError: null })
    try {
      const project = await (requireCreated
        ? services.createProject(name, orgId, { requireCreated: true })
        : services.createProject(name, orgId))
      const projectId = projectIdOf(project)
      if (!projectId) throw new Error('The workspace service returned a project without an id.')
      if (!isCurrent('project', request) || state.orgId !== orgId) return null
      // The create endpoint is idempotent, so reconcile a replay by identity.
      const nextProjects = state.projects.some((item) => projectIdOf(item) === projectId)
        ? state.projects.map((item) => projectIdOf(item) === projectId ? project : item)
        : [...state.projects, project]
      publish({ projects: nextProjects, projectsError: null, projectDraftError: null })
      invoke(callbacks.onProjectCreated, project)

      if (!isCurrent('project', request) || state.orgId !== orgId
        || generations.workspace !== workspaceRequest) {
        if (isCurrent('project', request)) publish({ projectBusy: false })
        return project
      }

      const openRequest = nextGeneration('workspace')
      resumeAttempted = true
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
        if (!state.mock) writeStoredProjectId(orgId, projectId, storage)
        publish({ workspace, workspaceLoading: false })
        invoke(callbacks.onWorkspaceOpened, { projectId, orgId, workspace })
      } catch {
        if (isCurrent('workspace', openRequest) && state.openProjectId === projectId && state.orgId === orgId) {
          publish({ workspace: null, workspaceError: humanizeError(null), workspaceLoading: false })
        }
      } finally {
        if (isCurrent('project', request)) publish({ projectBusy: false })
      }
      return project
    } catch (error) {
      if (!isCurrent('project', request)) return null
      const message = error?.code === 'project_name_collision' ? error.message : explain(error)
      publish({ projectDraftError: message, projectsError: message, projectBusy: false })
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
    resumeAttempted = false
    publish({
      mock: value,
      bootstrapState: 'unknown',
      bootstrapMessage: null,
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
      orgErrorStatus: null,
      autoProvision: 'idle',
      autoProvisionError: null,
    })
  }

  function setReadsPaused(next) { readsPaused = !!next }

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
    autoProvisionOrg,
    retryAutoProvision,
    createProject,
    selectCanonicalVersion,
    setMock,
    setReadsPaused,
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
