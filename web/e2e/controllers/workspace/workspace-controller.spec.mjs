import { expect, test } from '@playwright/test'

import {
  WORKSPACE_ORG_KEY,
  createWorkspaceController,
  selectCanonicalVersion,
  selectCurrentProjectName,
} from '../../../src/controllers/workspace/createWorkspaceController.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  const writes = []
  return {
    writes,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { writes.push(['set', key, value]); values.set(key, value) },
    removeItem: (key) => { writes.push(['remove', key]); values.delete(key) },
  }
}

test('reads the stored org and ignores a stale project list response', async () => {
  const firstList = deferred()
  const storage = memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-old' })
  const controller = createWorkspaceController({
    storage,
    services: {
      listProjects: () => firstList.promise,
      createOrg: async () => ({ org_id: 'org-new', name: 'New org' }),
    },
  })

  expect(controller.getSnapshot().orgId).toBe('org-old')
  const loading = controller.loadProjects()
  await controller.createOrg('New org')
  firstList.resolve([{ project_id: 'stale', name: 'Stale project' }])
  await loading

  const state = controller.getSnapshot()
  expect(state.orgId).toBe('org-new')
  expect(state.projects).toEqual([])
  expect(storage.writes).toEqual([['set', WORKSPACE_ORG_KEY, 'org-new']])
})

test('adopts the session org and invalidates stale workspace state', async () => {
  const listing = deferred()
  const workspace = deferred()
  const storage = memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-old' })
  const controller = createWorkspaceController({
    storage,
    services: {
      listProjects: () => listing.promise,
      openProject: () => workspace.promise,
    },
  })

  const loading = controller.loadProjects()
  const opening = controller.openProject('project-old')
  expect(controller.adoptOrgId('org-session')).toBe(true)
  listing.resolve([{ project_id: 'project-old', name: 'Old project' }])
  workspace.resolve({ project: { project_id: 'project-old', name: 'Old project' } })
  await Promise.all([loading, opening])

  expect(controller.getSnapshot()).toMatchObject({
    orgId: 'org-session',
    projects: [],
    openProjectId: null,
    workspace: null,
  })
  expect(storage.writes).toEqual([['set', WORKSPACE_ORG_KEY, 'org-session']])
  expect(controller.adoptOrgId('org-session')).toBe(true)
  expect(storage.writes).toHaveLength(1)
  expect(controller.adoptOrgId('  ')).toBe(false)
})

test('latest open wins and close invalidates a pending hydration', async () => {
  const alpha = deferred()
  const beta = deferred()
  const services = {
    openProject: (projectId) => projectId === 'alpha' ? alpha.promise : beta.promise,
  }
  const controller = createWorkspaceController({ services, storage: memoryStorage() })

  const openingAlpha = controller.openProject('alpha')
  const openingBeta = controller.openProject('beta')
  alpha.resolve({ project: { project_id: 'alpha', name: 'Alpha' } })
  await openingAlpha
  expect(controller.getSnapshot().workspace).toBeNull()

  controller.closeProject()
  beta.resolve({ project: { project_id: 'beta', name: 'Beta' } })
  await openingBeta
  expect(controller.getSnapshot()).toMatchObject({
    openProjectId: null,
    workspace: null,
    workspaceLoading: false,
  })
})

test('create project appends before open and exposes canonical selection', async () => {
  const order = []
  const storage = memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-1' })
  const controller = createWorkspaceController({
    storage,
    services: {
      createProject: async (name, orgId) => {
        order.push(['create', name, orgId])
        return { project_id: 'project-1', name }
      },
      openProject: async (projectId, orgId) => {
        order.push(['open', projectId, orgId, controller.getSnapshot().projects.length])
        return {
          project: { project_id: projectId, name: 'Rooftop' },
          drawing_versions: [{ version_id: 'version-1', drawing_id: 'drawing-1', seq: 1 }],
        }
      },
    },
  })

  await controller.createProject('  Rooftop  ')
  controller.selectCanonicalVersion('version-1')

  expect(order).toEqual([
    ['create', 'Rooftop', 'org-1'],
    ['open', 'project-1', 'org-1', 1],
  ])
  expect(selectCurrentProjectName(controller.getSnapshot())).toBe('Rooftop')
  expect(selectCanonicalVersion(controller.getSnapshot())).toMatchObject({
    version_id: 'version-1',
    drawing_id: 'drawing-1',
  })
})

test('replaying an idempotent project response keeps one canonical project', async () => {
  let replay = 0
  const controller = createWorkspaceController({
    storage: memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-1' }),
    services: {
      createProject: async () => ({ project_id: 'project-1', name: replay++ ? 'SoundBeam' : 'Initial name' }),
      openProject: async () => ({ project: { project_id: 'project-1', name: 'SoundBeam' } }),
    },
  })

  await controller.createProject('SoundBeam')
  await controller.createProject('SoundBeam')

  expect(controller.getSnapshot().projects).toEqual([
    { project_id: 'project-1', name: 'SoundBeam' },
  ])
})

test('rehydration keeps the last good workspace when refresh fails', async () => {
  let reads = 0
  const initial = { project: { project_id: 'project-1', name: 'Stable' }, jobs: [] }
  const controller = createWorkspaceController({
    storage: memoryStorage(),
    formatError: () => 'calm error',
    services: {
      openProject: async () => {
        reads += 1
        if (reads === 1) return initial
        throw new Error('temporary failure')
      },
    },
  })

  await controller.openProject('project-1')
  await controller.rehydrate()

  expect(controller.getSnapshot()).toMatchObject({
    workspace: initial,
    workspaceLoading: false,
    projectsError: null,
  })
})

test('mock mode makes project-list loading a no-op', async () => {
  let reads = 0
  const controller = createWorkspaceController({
    mock: true,
    storage: memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-1' }),
    services: { listProjects: async () => { reads += 1; return [] } },
  })

  await controller.loadProjects()
  expect(reads).toBe(0)
  expect(controller.getSnapshot()).toMatchObject({ projects: [], projectsError: null })
})

test('list and open failures settle loading state with a formatted error', async () => {
  const storage = memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-1' })
  const controller = createWorkspaceController({
    storage,
    formatError: () => 'Workspace unavailable. Try again.',
    services: {
      listProjects: async () => { throw new Error('GET /api/projects -> 500') },
      openProject: async () => { throw new Error('GET /api/projects/p-1 -> 500') },
    },
  })

  const listing = controller.loadProjects()
  expect(controller.getSnapshot().projectsLoading).toBe(true)
  await listing
  expect(controller.getSnapshot()).toMatchObject({
    projects: [],
    projectsLoading: false,
    projectsError: 'Workspace unavailable. Try again.',
  })

  const opening = controller.openProject('p-1')
  expect(controller.getSnapshot()).toMatchObject({
    openProjectId: 'p-1',
    workspace: null,
    workspaceLoading: true,
    projectsError: null,
  })
  await opening
  expect(controller.getSnapshot()).toMatchObject({
    workspace: null,
    workspaceLoading: false,
    projectsError: 'Workspace unavailable. Try again.',
  })
})

test('a superseded project hydration still settles its create busy state', async () => {
  const createdWorkspace = deferred()
  const otherWorkspace = deferred()
  const controller = createWorkspaceController({
    storage: memoryStorage({ [WORKSPACE_ORG_KEY]: 'org-1' }),
    services: {
      createProject: async () => ({ project_id: 'created', name: 'Created' }),
      openProject: (projectId) => projectId === 'created'
        ? createdWorkspace.promise
        : otherWorkspace.promise,
    },
  })

  const creating = controller.createProject('Created')
  await Promise.resolve()
  const openingOther = controller.openProject('other')
  createdWorkspace.resolve({ project: { project_id: 'created', name: 'Created' } })
  await creating
  expect(controller.getSnapshot()).toMatchObject({
    openProjectId: 'other',
    workspace: null,
    projectBusy: false,
  })

  otherWorkspace.resolve({ project: { project_id: 'other', name: 'Other' } })
  await openingOther
  expect(selectCurrentProjectName(controller.getSnapshot())).toBe('Other')
})
