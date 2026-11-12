import { describe, expect, it, vi } from 'vitest'
import { attachDrawingToProject, createProjectFromDrawing, drawingAttachKey, mountedDrawingSource } from './createProjectFromDrawing.js'

const source = Object.freeze({ drawingId: 'd1', version: 3, name: 'walk.dxf' })
const importArgs = ['p1', source, { idempotencyKey: 'b3:p1:d1:3' }]

describe('mountedDrawingSource', () => {
  const mounted = { drawingState: { drawing_id: 'd1', head: 3 }, requestedDrawingId: 'd1', name: ' walk.dxf ' }

  it('captures a matching account head with a trimmed name and freezes it', () => {
    const captured = mountedDrawingSource(mounted)
    expect(captured).toEqual(source)
    expect(Object.isFrozen(captured)).toBe(true)
  })

  it('rejects mismatched ids, invalid ids and invalid heads', () => {
    expect(mountedDrawingSource()).toBeNull()
    expect(mountedDrawingSource({ ...mounted, requestedDrawingId: 'd2' })).toBeNull()
    for (const head of [undefined, 0, -1, 1.5, '3']) {
      expect(mountedDrawingSource({ ...mounted, drawingState: { drawing_id: 'd1', head } })).toBeNull()
    }
    for (const drawingId of [undefined, null, 42, '', '   ']) {
      expect(mountedDrawingSource({ ...mounted, requestedDrawingId: drawingId,
        drawingState: { drawing_id: drawingId, head: 3 } })).toBeNull()
    }
  })

  it('maps blank and non-string names to null', () => {
    for (const name of ['', '  ', undefined, null, 42]) {
      expect(mountedDrawingSource({ ...mounted, name }).name).toBeNull()
    }
  })
})

describe('createProjectFromDrawing', () => {
  it('creates once and attaches the drawing using the upload key', async () => {
    const project = { project_id: 'p1' }
    const drawingVersion = { version_id: 'v1' }
    const createProject = vi.fn().mockResolvedValue(project)
    const importUpload = vi.fn().mockResolvedValue(drawingVersion)
    const result = await createProjectFromDrawing({ name: 'Maple', source, createProject, importUpload })
    expect(createProject).toHaveBeenCalledExactlyOnceWith('Maple')
    expect(importUpload).toHaveBeenCalledExactlyOnceWith(...importArgs)
    expect(result).toEqual({ project, attached: true, drawingVersion })
    expect(drawingAttachKey('p1', source)).toBe('b3:p1:d1:3')
  })

  it('keeps click custody while project creation is pending', async () => {
    let resolveProject
    const createProject = vi.fn(() => new Promise((resolve) => { resolveProject = resolve }))
    const importUpload = vi.fn().mockResolvedValue({})
    let mounted = { drawingState: { drawing_id: 'd1', head: 3 }, requestedDrawingId: 'd1', name: 'walk.dxf' }
    const captured = mountedDrawingSource(mounted)
    const pending = createProjectFromDrawing({ name: 'Maple', source: captured, createProject, importUpload })
    mounted = { drawingState: { drawing_id: 'd2', head: 4 }, requestedDrawingId: 'd2', name: 'other.dxf' }
    expect(mountedDrawingSource(mounted).drawingId).toBe('d2')
    resolveProject({ project_id: 'p1' })
    await pending
    expect(importUpload).toHaveBeenCalledExactlyOnceWith(...importArgs)
  })

  it('returns null without importing when creation returns null', async () => {
    const createProject = vi.fn().mockResolvedValue(null)
    const importUpload = vi.fn()
    expect(await createProjectFromDrawing({ name: 'Maple', source, createProject, importUpload })).toBeNull()
    expect(createProject).toHaveBeenCalledTimes(1)
    expect(importUpload).not.toHaveBeenCalled()
  })

  it('accepts a project with id only', async () => {
    const project = { id: 'p1' }
    const importUpload = vi.fn().mockResolvedValue({})
    const result = await createProjectFromDrawing({ name: 'Maple', source,
      createProject: vi.fn().mockResolvedValue(project), importUpload })
    expect(result.project).toBe(project)
    expect(importUpload).toHaveBeenCalledExactlyOnceWith(...importArgs)
  })

  it('rejects a project without an id before importing', async () => {
    const importUpload = vi.fn()
    await expect(createProjectFromDrawing({ name: 'Maple', source,
      createProject: vi.fn().mockResolvedValue({}), importUpload }))
      .rejects.toThrow('The workspace service returned a project without an id.')
    expect(importUpload).not.toHaveBeenCalled()
  })

  it('keeps the project on attach failure and retries twice without another create', async () => {
    const project = { project_id: 'p1' }
    const error = new Error('Upload unavailable')
    const drawingVersion = { version_id: 'v1' }
    const createProject = vi.fn().mockResolvedValue(project)
    const importUpload = vi.fn().mockRejectedValueOnce(error).mockResolvedValue(drawingVersion)
    const result = await createProjectFromDrawing({ name: 'Maple', source, createProject, importUpload })
    expect(result).toEqual({ project, attached: false, reason: 'attach-failed', error, retry: expect.any(Function) })
    expect(createProject).toHaveBeenCalledTimes(1)
    expect(importUpload).toHaveBeenCalledTimes(1)
    expect(await result.retry()).toBe(drawingVersion)
    expect(await result.retry()).toBe(drawingVersion)
    expect(createProject).toHaveBeenCalledTimes(1)
    expect(importUpload.mock.calls).toEqual([importArgs, importArgs, importArgs])
  })

  it('keeps the project when the captured drawing is unattachable', async () => {
    const project = { project_id: 'p1' }
    const createProject = vi.fn().mockResolvedValue(project)
    const importUpload = vi.fn()
    expect(await createProjectFromDrawing({ name: 'Maple', source: null, createProject, importUpload }))
      .toEqual({ project, attached: false, reason: 'unattachable' })
    expect(createProject).toHaveBeenCalledTimes(1)
    expect(importUpload).not.toHaveBeenCalled()
  })

  it('returns the import result directly from attachDrawingToProject', async () => {
    const drawingVersion = { version_id: 'v1' }
    const importUpload = vi.fn().mockResolvedValue(drawingVersion)
    expect(await attachDrawingToProject({ projectId: 'p1', source, importUpload })).toBe(drawingVersion)
    expect(importUpload).toHaveBeenCalledExactlyOnceWith(...importArgs)
  })
})
