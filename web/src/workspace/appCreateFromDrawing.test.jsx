// @vitest-environment jsdom
// Actual drawing-only slot, production App callback text, identity and workspace
// controller. The import service is synthetic source proof, never a live upload.
import {readFileSync} from 'node:fs'
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {afterEach, describe, expect, it, vi} from 'vitest'
import {WorkspaceProjectSlot} from '../components/ProductSurfaceTabs.jsx'
import {deriveWorkspaceProjectState} from '../site/workspaceProjectState.js'
import {identityFromUploadReceipt} from '../drawing/drawingIdentity.js'
import {DrawingIdentityProvider, useDrawingIdentity} from '../drawing/DrawingIdentityProvider.jsx'
import {createWorkspaceController} from '../controllers/workspace/createWorkspaceController.js'

const app = readFileSync('src/App.jsx', 'utf8')
const promotion = app.match(/function promoteStandaloneUpload[\s\S]*?(?=\nexport function StandaloneMaterialUpload)/)?.[0]
const caller = app.match(/const onCreateProject = useCallback\([\s\S]*?\n  \}, \[[^\n]*\]\)/)?.[0]
if (!promotion || !caller) throw new Error('Production caller extraction failed')
const promote = new Function(`${promotion}; return promoteStandaloneUpload`)()
const ID = 'upload-real', ORG = 'org-real', PROJECT = 'project-real', VERSION = 'version-real'
const uploaded = () => identityFromUploadReceipt({drawing_id: ID, tenant_kind: 'account', extracted_version: 7})
const row = {version_id: VERSION, project_id: PROJECT, org_id: ORG, drawing_id: 'canonical-artifact'}
const defer = () => {let resolve; const promise = new Promise(r => {resolve = r}); return {promise, resolve}}

function setup() {
  let current = true
  const order = []
  const services = {
    createProject: vi.fn(async () => {order.push('create'); return {project_id: PROJECT, name: 'Original'}}),
    importUpload: vi.fn(async () => {order.push('import'); return {drawingVersion: row, replayed: false}}),
    openProject: vi.fn(async () => {order.push('open'); return {project: {project_id: PROJECT, org_id: ORG}, drawing_versions: [row]}}),
    listProjects: vi.fn(async () => []),
  }
  const controller = createWorkspaceController({services, storage: {getItem: () => ORG, setItem: vi.fn(), removeItem: vi.fn()}})
  const env = {createWorkspaceProject: controller.createProject, uploadSource: uploaded()?.uploadSource,
    mock: false, isScopeCurrent: () => current, REQUESTED_DRAWING_ID: ID,
    requestedDrawingIdRef: {current: ID}, setProjectPane: vi.fn()}
  const build = () => new Function(...Object.keys(env), 'useCallback', `${caller};return onCreateProject`)(...Object.values(env), f => f)
  return {services, controller, env, build, order, retire: () => {current = false}}
}
afterEach(cleanup)

describe('create project from actual uploaded drawing', () => {
  it('preserves positive extraction provenance through promotion and provider, then clears it on scope reset', async () => {
    const token = {}, selection = {}, set = vi.fn()
    promote({receipt: {drawing_id: ID, tenant_kind: 'account'}, status: {extracted_version: 7}},
      {token, selection}, {token, selection, pane: 'material'}, set)
    expect(set).toHaveBeenCalledWith({drawing_id: ID, tenant_kind: 'account', extracted_version: 7})
    let identity
    function Probe() {identity = useDrawingIdentity(); return null}
    render(<DrawingIdentityProvider mode="console" scene="app" search="" publicDemo={false} liveDemo={false}
      readAuthToken={() => null} subscribeAuthChange={() => () => {}} rememberDrawingId={() => {}} forgetDrawingId={() => {}}>
      <Probe />
    </DrawingIdentityProvider>)
    act(() => identity.setFromUpload(set.mock.calls[0][0]))
    expect(identity.uploadSource).toEqual({drawingId: ID, version: 7})
    expect(Object.isFrozen(identity.uploadSource)).toBe(true)
    act(() => identity.reset())
    expect(identity.uploadSource).toBeNull()
  })

  it('actual visible drawing-only action imports the captured version before opening and selects canonical version', async () => {
    const s = setup()
    render(<WorkspaceProjectSlot state={deriveWorkspaceProjectState({drawingName: ID, orgId: ORG})} onCreateProject={s.build()} />)
    fireEvent.click(screen.getByRole('button', {name: 'Create project from this drawing'}))
    await waitFor(() => expect(s.controller.getSnapshot().workspaceLoading).toBe(false))
    await waitFor(() => expect(s.services.openProject).toHaveBeenCalledTimes(1))
    expect(s.services.importUpload).toHaveBeenCalledWith(PROJECT, {drawingId: ID, version: 7, name: ID}, {idempotencyKey: `b3:${PROJECT}:${ID}:7`})
    expect(s.order).toEqual(['create','import','open'])
    expect(s.controller.getSnapshot()).toMatchObject({openProjectId: PROJECT, canonicalVersionId: VERSION, workspace: {drawing_versions: [row]}})
  })

  it.each([null, {}, {drawing_id: ID, tenant_kind: 'guest', extracted_version: 7}, {drawing_id: ID, tenant_kind: 'account', extracted_version: 0}])('unknown, sample or guest provenance opens normal intake without an empty project', async receipt => {
    const s = setup(); s.env.uploadSource = identityFromUploadReceipt(receipt)?.uploadSource
    expect(await s.build()('Original', {fromDrawing: true})).toBeNull()
    expect(s.env.setProjectPane).toHaveBeenCalledWith('material')
    expect(s.services.createProject).not.toHaveBeenCalled()
    expect(s.services.importUpload).not.toHaveBeenCalled()
  })

  it('plain create remains an empty project operation', async () => {
    const s = setup(); await s.build()('Original')
    expect(s.services.createProject).toHaveBeenCalledTimes(1)
    expect(s.services.importUpload).not.toHaveBeenCalled()
  })

  it('explicit retry reuses the created project and the same canonical import key after failure', async () => {
    const s = setup(); s.services.importUpload.mockRejectedValueOnce(new Error('Import unavailable'))
    expect(await s.build()('Original', {fromDrawing: true})).toBeNull()
    expect(s.services.openProject).not.toHaveBeenCalled()
    expect(s.controller.getSnapshot().projectBusy).toBe(false)
    await s.build()('Original', {fromDrawing: true})
    expect(s.services.createProject).toHaveBeenCalledTimes(1)
    expect(s.services.importUpload).toHaveBeenCalledTimes(2)
    expect(s.services.importUpload.mock.calls[0][2]).toEqual(s.services.importUpload.mock.calls[1][2])
    expect(s.controller.getSnapshot().canonicalVersionId).toBe(VERSION)
  })

  it.each(['org','project','drawing'])('scope change during create cannot import into stale %s', async kind => {
    const s = setup(), pending = defer()
    s.services.createProject.mockReturnValueOnce(pending.promise)
    const run = s.build()('Original', {fromDrawing: true})
    if (kind === 'org') s.controller.adoptOrgId('other-org')
    if (kind === 'project') s.controller.closeProject()
    if (kind === 'drawing') s.env.requestedDrawingIdRef.current = 'other-upload'
    pending.resolve({project_id: PROJECT}); await run
    expect(s.services.importUpload).not.toHaveBeenCalled()
    expect(s.controller.getSnapshot().openProjectId).toBeNull()
    expect(s.controller.getSnapshot().projectBusy).toBe(false)
  })

  it('duplicate click during create admits only one project and import', async () => {
    const s = setup(), pending = defer(); s.services.createProject.mockReturnValueOnce(pending.promise)
    const run = s.build()('Original', {fromDrawing: true})
    expect(await s.build()('Original', {fromDrawing: true})).toBeNull()
    pending.resolve({project_id: PROJECT}); await run
    expect(s.services.createProject).toHaveBeenCalledTimes(1)
    expect(s.services.importUpload).toHaveBeenCalledTimes(1)
  })

  it('a foreign import receipt never becomes selected project state', async () => {
    const s = setup(); s.services.importUpload.mockResolvedValueOnce({drawingVersion: {...row, project_id: 'foreign'}, replayed: false})
    expect(await s.build()('Original', {fromDrawing: true})).toBeNull()
    expect(s.services.openProject).not.toHaveBeenCalled()
    expect(s.controller.getSnapshot().canonicalVersionId).toBeNull()
  })

  it('project switch while import is pending cannot open the import target afterward', async () => {
    const s = setup(), pending = defer(); s.services.importUpload.mockReturnValueOnce(pending.promise)
    const run = s.build()('Original', {fromDrawing: true})
    await waitFor(() => expect(s.services.importUpload).toHaveBeenCalled())
    s.controller.closeProject(); pending.resolve({drawingVersion: row, replayed: false}); await run
    expect(s.services.openProject).not.toHaveBeenCalled()
    expect(s.controller.getSnapshot().projectBusy).toBe(false)
  })
})
