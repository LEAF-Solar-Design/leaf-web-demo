import { describe, expect, it, vi } from 'vitest'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import {
  SAMPLE_ID_PREFIX, SAMPLE_ORG, SAMPLE_PROJECTS, SAMPLE_IOS_STATES,
  isSampleId, createFixtureServices, iosSurfaceFixture,
} from './workspaceFixture.js'
import { IOS_SURFACE_CONTRACT_SCHEMA, fetchIosSurfaceStatus } from '../ios/iosSurfaceStatus.js'
import { deriveIosState, IOS_STATE_LABEL } from '../ios/IosSurface.jsx'

const src = existsSync(path.resolve(process.cwd(), 'src/demo/workspaceFixture.js'))
  ? path.resolve(process.cwd(), 'src') : path.resolve(process.cwd(), 'web/src')
const realId = '550e8400-e29b-41d4-a716-446655440000'

function walk(value, visit) {
  if (!value || typeof value !== 'object') return
  for (const [key, child] of Object.entries(value)) {
    visit(key, child)
    walk(child, visit)
  }
}

describe('held sample workspace', () => {
  it('D1 row1: every emitted identifier has the reserved prefix', async () => {
    const services = createFixtureServices()
    const org = await services.createOrg('New organization')
    const project = await services.createProject('New project', org.org_id)
    const data = [SAMPLE_ORG, SAMPLE_PROJECTS, org, project,
      await services.getOrgIdentities(SAMPLE_ORG.org_id),
      ...SAMPLE_IOS_STATES.map(iosSurfaceFixture)]
    let ids = 0
    walk(data, (key, value) => {
      if (key === 'id' || key.endsWith('_id') || key.endsWith('Id')) {
        ids += 1
        expect(isSampleId(value), key).toBe(true)
      }
      if (key === 'name') expect(value).toMatch(/^Sample: /)
    })
    expect(ids).toBeGreaterThan(20)
  })

  it('D1 row2: only prefixed strings are sample identifiers', () => {
    expect(SAMPLE_ID_PREFIX).toBe('sample-')
    for (const value of [realId, 1, '', null, {}, undefined, 'Sample-project', 'xsample-id']) {
      expect(isSampleId(value)).toBe(false)
    }
    for (const value of ['sample-', 'sample-project']) expect(isSampleId(value)).toBe(true)
  })

  it('D1 row3: fixture source has no transport or persistence capability', () => {
    const source = readFileSync(path.join(src, 'demo/workspaceFixture.js'), 'utf8')
    expect(source).not.toMatch(/fetch|XMLHttpRequest|localStorage|sessionStorage/)
    expect(source).not.toMatch(/\bimport\b/)
    expect(source).not.toMatch(/setTimeout|setInterval/)
  })

  it('D1 row4: every ID-taking service refuses live identifiers', async () => {
    const services = createFixtureServices()
    // createOrg(name) takes a name, not an identifier. All eight ID-taking
    // services are exercised, including both ID slots of openProject.
    const calls = [
      () => services.listProjects(realId),
      () => services.createProject('A name', realId),
      () => services.openProject(realId, SAMPLE_ORG.org_id),
      () => services.openProject(SAMPLE_PROJECTS[0].project_id, realId),
      () => services.getProjectLifecycle(realId),
      () => services.getOrgIdentities(realId),
      () => services.listDrawingVersions(realId),
      () => services.listJobs(realId),
      () => services.getReceipt(realId),
    ]
    for (const call of calls) await expect(call()).rejects.toMatchObject({ name: 'SampleIdError' })
    await expect(services.getProjectLifecycle('sample-absent')).rejects.toMatchObject({ name: 'SampleNotFoundError' })
  })

  it('D1 row5: worked and empty projects resolve coherent records', async () => {
    const services = createFixtureServices()
    const [worked, empty] = await services.listProjects(SAMPLE_ORG.org_id)
    const workspace = await services.openProject(worked.project_id, SAMPLE_ORG.org_id)
    const versions = await services.listDrawingVersions(worked.project_id)
    const jobs = await services.listJobs(worked.project_id)
    const receipt = await services.getReceipt(jobs[0].receipt_id)
    const lifecycle = await services.getProjectLifecycle(worked.project_id)
    const roster = await services.getOrgIdentities(SAMPLE_ORG.org_id)
    expect(versions[0].drawing_id).toBe(workspace.drawings[0].drawing_id)
    expect(jobs[0].version_id).toBe(versions[0].version_id)
    expect(jobs[0].status).toBe('succeeded')
    expect(receipt.job_id).toBe(jobs[0].job_id)
    expect(lifecycle.receipts).toContainEqual(receipt)
    expect(roster.identities.map((item) => item.binding_id)).toEqual(lifecycle.members.map((item) => item.binding_id))
    expect(worked.native_revisions[0].status).toBe('approved')
    expect((await services.openProject(empty.project_id, SAMPLE_ORG.org_id)).drawings).toEqual([])
    expect(await services.listDrawingVersions(empty.project_id)).toEqual([])
    expect(await services.listJobs(empty.project_id)).toEqual([])
    const blank = await services.getProjectLifecycle(empty.project_id)
    for (const key of ['members', 'files', 'receipts']) expect(blank[key]).toEqual([])
    workspace.drawings[0].name = 'Changed locally'
    expect((await services.openProject(worked.project_id, SAMPLE_ORG.org_id)).drawings[0].name).toMatch(/^Sample: /)
  })

  it('D1 row6: creation returns distinct sample records isolated to one instance', async () => {
    const services = createFixtureServices()
    const orgs = [await services.createOrg('A'), await services.createOrg('A')]
    const projects = [await services.createProject('B', orgs[0].org_id), await services.createProject('B', orgs[0].org_id)]
    const ids = [...orgs.map((org) => org.org_id), ...projects.map((project) => project.project_id)]
    expect(ids.every(isSampleId)).toBe(true)
    expect(new Set(ids).size).toBe(4)
    expect(await services.listProjects(orgs[0].org_id)).toHaveLength(2)
    expect(await services.getOrgIdentities(orgs[0].org_id)).toEqual({ sample: true, identities: [] })
    projects[0].name = 'Changed locally'
    expect((await services.listProjects(orgs[0].org_id))[0].name).toBe('Sample: B')
    expect(await createFixtureServices().listProjects(SAMPLE_ORG.org_id)).toEqual(SAMPLE_PROJECTS)
    await expect(createFixtureServices().listProjects(orgs[0].org_id)).rejects.toMatchObject({ name: 'SampleNotFoundError' })
  })

  it('D1 row7: each named iOS state follows the existing surface contract', async () => {
    expect(SAMPLE_IOS_STATES.slice().sort()).toEqual(Object.keys(IOS_STATE_LABEL).sort())
    for (const state of SAMPLE_IOS_STATES) {
      const contract = iosSurfaceFixture(state)
      expect(deriveIosState(contract)).toBe(state)
      // Never configured is represented by absence in the real reader.
      if (state === 'never-configured') expect(contract).toBeNull()
      else expect(contract.schema).toBe(IOS_SURFACE_CONTRACT_SCHEMA)
      const fetchImpl = vi.fn(async () => ({
        ok: true,
        json: async () => contract ? { status: 'available', contract } : { status: 'unavailable' },
      }))
      expect(await fetchIosSurfaceStatus({ projectId: SAMPLE_PROJECTS[0].project_id, revision: 'sample-native-revision-1', fetchImpl })).toEqual(contract)
    }
    expect(() => iosSurfaceFixture('unknown')).toThrowError(expect.objectContaining({ name: 'SampleStateError' }))
  })

  it('D1 row10: the fixture and hook are imported only by the owned files', () => {
    const allowed = new Set(['demo/workspaceFixture.js', 'demo/useWorkspaceFixture.js', 'demo/workspaceFixture.test.js', 'demo/useWorkspaceFixture.test.jsx'])
    const hits = []
    function scan(dir) {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const file = path.join(dir, entry.name)
        if (entry.isDirectory()) scan(file)
        else if (/\.[cm]?[jt]sx?$/.test(entry.name)) {
          const source = readFileSync(file, 'utf8')
          const imports = /(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['"][^'"]*(?:useWorkspaceFixture|workspaceFixture)(?:\.[^'"]*)?['"]/g
          if (imports.test(source)) hits.push(path.relative(src, file).split(path.sep).join('/'))
        }
      }
    }
    scan(src)
    expect(hits.length).toBeGreaterThan(0)
    expect(hits.filter((file) => !allowed.has(file))).toEqual([])
  })
})
