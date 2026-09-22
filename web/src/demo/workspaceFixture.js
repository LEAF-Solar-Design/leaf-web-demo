// W4h D1: held sample workspace. All state belongs to one service instance.
export const SAMPLE_ID_PREFIX = 'sample-'
export const isSampleId = (value) => typeof value === 'string' && value.startsWith(SAMPLE_ID_PREFIX)

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}

const copy = (value) => JSON.parse(JSON.stringify(value))

export const SAMPLE_ORG = freeze({
  org_id: 'sample-org-workspace', name: 'Sample: Design workspace', sample: true,
})

function emptyProject(project_id, org_id, name) {
  return {
    project_id, org_id, name, sample: true, status: 'active', profile: 'cad',
    drawings: [], drawing_versions: [], jobs: [], receipts: [], members: [],
    native_revisions: [],
  }
}

export const SAMPLE_PROJECTS = freeze([
  {
    ...emptyProject('sample-project-rooftop', SAMPLE_ORG.org_id, 'Sample: Rooftop Retail'),
    drawings: [{ sample: true, drawing_id: 'sample-drawing-roof', project_id: 'sample-project-rooftop', name: 'Sample: Roof plan' }],
    drawing_versions: [{ sample: true, version_id: 'sample-version-roof-1', drawing_id: 'sample-drawing-roof', project_id: 'sample-project-rooftop', name: 'Sample: Roof revision 1', version: 1 }],
    jobs: [{ sample: true, job_id: 'sample-job-layout', project_id: 'sample-project-rooftop', version_id: 'sample-version-roof-1', receipt_id: 'sample-receipt-layout', name: 'Sample: Layout check', status: 'succeeded' }],
    receipts: [{ sample: true, receipt_id: 'sample-receipt-layout', job_id: 'sample-job-layout', project_id: 'sample-project-rooftop', name: 'Sample: Layout result', status: 'succeeded' }],
    members: [
      { sample: true, membership_id: 'sample-membership-owner', binding_id: 'sample-binding-owner', project_id: 'sample-project-rooftop', name: 'Sample: Owner', role: 'owner', status: 'active' },
      { sample: true, membership_id: 'sample-membership-reviewer', binding_id: 'sample-binding-reviewer', project_id: 'sample-project-rooftop', name: 'Sample: Reviewer', role: 'reviewer', status: 'active' },
    ],
    native_revisions: [{ sample: true, revision_id: 'sample-native-revision-1', project_id: 'sample-project-rooftop', name: 'Sample: Approved native revision', status: 'approved' }],
  },
  emptyProject('sample-project-empty', SAMPLE_ORG.org_id, 'Sample: Empty project'),
])

function fail(name, message) {
  const error = new Error(message)
  error.name = name
  throw error
}

function requireSampleId(id) {
  if (!isSampleId(id)) fail('SampleIdError', 'Only sample identifiers are accepted by this fixture.')
}

function sampleName(name) {
  if (typeof name !== 'string' || !name.trim()) {
    fail('SampleNameError', 'A sample record needs a name.')
  }
  return name.startsWith('Sample: ') ? name : `Sample: ${name.trim()}`
}

export function createFixtureServices() {
  const orgs = [copy(SAMPLE_ORG)]
  const projects = copy(SAMPLE_PROJECTS)
  let sequence = 0
  const nextId = (kind) => `${SAMPLE_ID_PREFIX}created-${kind}-${++sequence}`
  function orgFor(id) {
    requireSampleId(id)
    const org = orgs.find((item) => item.org_id === id)
    if (!org) fail('SampleNotFoundError', 'That sample organization does not exist.')
    return org
  }
  function projectFor(id) {
    requireSampleId(id)
    const project = projects.find((item) => item.project_id === id)
    if (!project) fail('SampleNotFoundError', 'That sample project does not exist.')
    return project
  }
  return {
    async createOrg(name) {
      const org = { org_id: nextId('org'), name: sampleName(name), sample: true }
      orgs.push(org)
      return copy(org)
    },
    async listProjects(orgId) {
      orgFor(orgId)
      return copy(projects.filter((project) => project.org_id === orgId))
    },
    async createProject(name, orgId) {
      orgFor(orgId)
      const project = emptyProject(nextId('project'), orgId, sampleName(name))
      projects.push(project)
      return copy(project)
    },
    async openProject(projectId, orgId) {
      orgFor(orgId)
      const project = projectFor(projectId)
      if (project.org_id !== orgId) fail('SampleNotFoundError', 'That sample project is not in this organization.')
      return copy({ sample: true, project, drawings: project.drawings, drawing_versions: project.drawing_versions })
    },
    async getProjectLifecycle(projectId) {
      const project = projectFor(projectId)
      return copy({ sample: true, project, members: project.members, files: project.drawings, receipts: project.receipts })
    },
    async getOrgIdentities(orgId) {
      orgFor(orgId)
      const members = projects.filter((project) => project.org_id === orgId).flatMap((project) => project.members)
      return { sample: true, identities: copy(members.map(({ binding_id, name }) => ({ sample: true, binding_id, org_id: orgId, name }))) }
    },
    async listDrawingVersions(projectId) {
      return copy(projectFor(projectId).drawing_versions)
    },
    async listJobs(projectId) {
      return copy(projectFor(projectId).jobs)
    },
    async getReceipt(receiptId) {
      requireSampleId(receiptId)
      const receipt = projects.flatMap((project) => project.receipts).find((item) => item.receipt_id === receiptId)
      if (!receipt) fail('SampleNotFoundError', 'That sample receipt does not exist.')
      return copy(receipt)
    },
  }
}

export const SAMPLE_IOS_STATES = freeze(['never-configured', 'unavailable', 'in-progress', 'ready'])

export function iosSurfaceFixture(state) {
  // Absence is the surface contract for never configured, not a readiness claim.
  if (state === 'never-configured') return null
  if (!SAMPLE_IOS_STATES.includes(state)) fail('SampleStateError', 'That sample setup state does not exist.')
  return {
    sample: true,
    schema: 'leaf.ios-ship-surface.v1',
    readiness: { sample: true, healthy: state !== 'unavailable', launchable: state === 'ready' },
    build_stage: state === 'in-progress' ? 'MAC_ALLOCATED' : null,
    receipt_id: 'sample-receipt-ios',
    reported_at: '2026-01-01T00:00:00Z',
  }
}
