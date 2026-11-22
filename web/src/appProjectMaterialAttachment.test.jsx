import { describe, expect, it, vi } from 'vitest'
import { captureProjectMaterialSelection, seatAttachedProjectDrawing,
  selectionForAttachedDrawing, selectionForProjectDrawingPreview } from './App.jsx'
import { createWorkspaceController } from './controllers/workspace/createWorkspaceController.js'
import { encodeVersionRef } from './lib/boardTransfer.js'

const ORG = '10000000-0000-4000-8000-000000000011'
const PROJECT = '20000000-0000-4000-8000-000000000011'
const DRAWING = '30000000-0000-4000-8000-000000000011'
const VERSION = '40000000-0000-4000-8000-000000000011'
const OTHER_PROJECT = '20000000-0000-4000-8000-000000000012'
const OTHER_ORG = '10000000-0000-4000-8000-000000000012'
const imported = { org_id: ORG, project_id: PROJECT, drawing_id: DRAWING, version_id: VERSION,
  seq: 1, name: 'Imported plan', oss_object: 'project/source.dwg', intake_ref: 'project/source.intake.json' }
const later = { ...imported, version_id: '40000000-0000-4000-8000-000000000012', seq: 2 }
const anotherDrawing = { ...imported, drawing_id: '30000000-0000-4000-8000-000000000012',
  version_id: '40000000-0000-4000-8000-000000000013' }
const workspace = (rows = []) => ({ project: { project_id: PROJECT, org_id: ORG }, drawing_versions: rows })
const snapshot = (rows = [], canonicalVersionId = null) => ({ orgId: ORG, openProjectId: PROJECT,
  canonicalVersionId, mock: false, workspace: workspace(rows) })
const reference = (changes = {}) => JSON.stringify({ kind: 'version', projectId: PROJECT,
  drawingId: DRAWING, versionId: VERSION, seq: 1, ...changes })

describe('material attachment selection decision', () => {
  it('selects the exact imported canonical version in a fresh project, even when a later head exists', () => {
    const before = snapshot()
    const after = snapshot([anotherDrawing, imported, later])
    expect(selectionForAttachedDrawing(imported, after, before)).toEqual({ ok: true, versionId: VERSION })
    expect(before.canonicalVersionId).toBeNull()
    expect(after.canonicalVersionId).toBeNull()
  })

  it('refuses a project or org switch instead of importing the old selection into the new scope', () => {
    for (const change of [{ openProjectId: OTHER_PROJECT }, { orgId: OTHER_ORG }, { mock: true }]) {
      expect(selectionForAttachedDrawing(imported, { ...snapshot([imported]), ...change }, snapshot()))
        .toMatchObject({ ok: false, reason: 'project-changed' })
    }
  })

  it('keeps a newer user selection, while allowing replacement of an unchanged prior selection', () => {
    expect(selectionForAttachedDrawing(imported, snapshot([imported, later], later.version_id), snapshot()))
      .toEqual({ ok: false, reason: 'selection-changed' })
    expect(selectionForAttachedDrawing(imported, snapshot([imported, later], later.version_id), snapshot([], later.version_id)))
      .toEqual({ ok: true, versionId: VERSION })
  })

  it('refuses unknown rows, mismatched exact identities, and incomplete attachment receipts', () => {
    expect(selectionForAttachedDrawing(imported, snapshot([later]), snapshot())).toEqual({ ok: false, reason: 'stale-version' })
    for (const change of [{ org_id: OTHER_ORG }, { project_id: OTHER_PROJECT },
      { drawing_id: anotherDrawing.drawing_id }, { version_id: later.version_id }, { seq: 2 }]) {
      expect(selectionForAttachedDrawing(imported, snapshot([{ ...imported, ...change }]), snapshot()).ok).toBe(false)
    }
    for (const row of [null, { ...imported, version_id: null }, { ...imported, seq: 0 },
      { ...imported, org_id: OTHER_ORG }, { ...imported, project_id: OTHER_PROJECT }]) {
      expect(selectionForAttachedDrawing(row, snapshot([imported]), snapshot()).ok).toBe(false)
    }
  })

  it('refuses workspace rows from a different scope even when the receipt appears in them', () => {
    for (const change of [{ project: { project_id: OTHER_PROJECT, org_id: ORG } },
      { project: { project_id: PROJECT, org_id: OTHER_ORG } }, { org_id: OTHER_ORG }]) {
      expect(selectionForAttachedDrawing(imported, { ...snapshot(), workspace: { ...workspace([imported]), ...change } }, snapshot()))
        .toEqual({ ok: false, reason: 'workspace-mismatch' })
    }
  })
})

async function attachmentLane() {
  const services = { openProject: vi.fn(async (projectId) => projectId === PROJECT ? workspace()
    : { project: { project_id: projectId, org_id: ORG }, drawing_versions: [] }) }
  const controller = createWorkspaceController({ services, storage: { getItem: () => null, setItem() {}, removeItem() {} } })
  controller.adoptOrgId(ORG)
  await controller.openProject(PROJECT)
  let session = { status: 'active', generation: 1 }
  const sessionController = { getSnapshot: () => session }
  const capture = captureProjectMaterialSelection(controller, sessionController)
  const select = vi.spyOn(controller, 'selectCanonicalVersion')
  let finish
  services.openProject.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  return { controller, capture, select, services, changeSession: (patch) => { session = { ...session, ...patch } },
    finish: (rows = [imported, later, anotherDrawing]) => finish(workspace(rows)) }
}

describe('material attachment guarded publication handoff', () => {
  it('hydrates and selects the receipt version through the workspace controller', async () => {
    const lane = await attachmentLane()
    const pending = seatAttachedProjectDrawing(lane.controller, imported, lane.capture)
    expect(lane.select).not.toHaveBeenCalled()
    lane.finish()
    expect(await pending).toBe(true)
    expect(lane.select).toHaveBeenCalledExactlyOnceWith(VERSION)
    expect(lane.controller.getSnapshot().canonicalVersionId).toBe(VERSION)
  })

  it('does not select a receipt absent from the refreshed workspace', async () => {
    const lane = await attachmentLane()
    const pending = seatAttachedProjectDrawing(lane.controller, imported, lane.capture)
    lane.finish([later])
    expect(await pending).toBe(false)
    expect(lane.select).not.toHaveBeenCalled()
  })

  it.each(['project', 'org', 'selection', 'session', 'signed-out', 'selection-away-and-back', 'project-away-and-back'])(
    'refuses deferred hydration after %s changes', async (change) => {
      const lane = await attachmentLane()
      const pending = seatAttachedProjectDrawing(lane.controller, imported, lane.capture)
      if (change === 'project' || change === 'project-away-and-back') {
        await lane.controller.openProject(OTHER_PROJECT)
        if (change === 'project-away-and-back') await lane.controller.openProject(PROJECT)
      }
      if (change === 'org') lane.controller.adoptOrgId(OTHER_ORG)
      if (change === 'session') lane.changeSession({ generation: 2 })
      if (change === 'signed-out') lane.changeSession({ status: 'required' })
      if (change === 'selection' || change === 'selection-away-and-back') {
        lane.controller.selectCanonicalVersion(later.version_id)
        if (change === 'selection-away-and-back') lane.controller.selectCanonicalVersion(null)
      }
      lane.select.mockClear()
      lane.finish()
      expect(await pending).toBe(false)
      expect(lane.select).not.toHaveBeenCalled()
      if (change === 'selection') expect(lane.controller.getSnapshot().canonicalVersionId).toBe(later.version_id)
    })

  it('preserves a selection made during upload, before the attachment callback even starts', async () => {
    const lane = await attachmentLane()
    lane.controller.selectCanonicalVersion(anotherDrawing.version_id)
    lane.select.mockClear()
    lane.services.openProject.mockClear()
    expect(await seatAttachedProjectDrawing(lane.controller, imported, lane.capture)).toBe(false)
    expect(lane.services.openProject).not.toHaveBeenCalled()
    expect(lane.select).not.toHaveBeenCalled()
    expect(lane.controller.getSnapshot().canonicalVersionId).toBe(anotherDrawing.version_id)
  })

  it('retires an unmounted material pane before its pending hydration resolves', async () => {
    const lane = await attachmentLane()
    const pending = seatAttachedProjectDrawing(lane.controller, imported, lane.capture)
    lane.capture.dispose()
    lane.finish()
    expect(await pending).toBe(false)
    expect(lane.select).not.toHaveBeenCalled()
  })
})

describe('project board preview reference validation', () => {
  it('selects a canonical version without any standalone drawing or existing selection', () => {
    expect(selectionForProjectDrawingPreview(reference(), snapshot([imported, later])))
      .toEqual({ ok: true, versionId: VERSION, seq: 1 })
    const raw = encodeVersionRef({ projectId: PROJECT, drawingId: anotherDrawing.drawing_id,
      versionId: anotherDrawing.version_id, seq: anotherDrawing.seq })
    expect(selectionForProjectDrawingPreview(raw, snapshot([imported, anotherDrawing], VERSION)))
      .toEqual({ ok: true, versionId: anotherDrawing.version_id, seq: 1 })
  })

  it('refuses forged or stale project, drawing, version, sequence and transfer shape', () => {
    for (const change of [{ projectId: OTHER_PROJECT }, { drawingId: anotherDrawing.drawing_id },
      { versionId: later.version_id }, { seq: 2 }, { kind: 'job' }, { extra: 'unexpected' }]) {
      expect(selectionForProjectDrawingPreview(reference(change), snapshot([imported])).ok).toBe(false)
    }
    for (const raw of [null, '{}', 'not-json', 'x'.repeat(1025)]) {
      expect(selectionForProjectDrawingPreview(raw, snapshot([imported])).ok).toBe(false)
    }
    expect(selectionForProjectDrawingPreview(reference(), snapshot([])).ok).toBe(false)
  })

  it('refuses foreign org/project rows and a workspace left over from another project', () => {
    for (const patch of [{ org_id: OTHER_ORG }, { project_id: OTHER_PROJECT }]) {
      expect(selectionForProjectDrawingPreview(reference(), snapshot([{ ...imported, ...patch }])).ok).toBe(false)
    }
    expect(selectionForProjectDrawingPreview(reference(), { ...snapshot([imported]), orgId: OTHER_ORG }).ok).toBe(false)
    expect(selectionForProjectDrawingPreview(reference(), { ...snapshot([imported]), openProjectId: OTHER_PROJECT }).ok).toBe(false)
    expect(selectionForProjectDrawingPreview(reference(), { ...snapshot([imported]), mock: true }).ok).toBe(false)
  })
})
