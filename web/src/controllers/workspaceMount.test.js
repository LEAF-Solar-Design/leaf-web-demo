// @vitest-environment node
//
// The merged workspace-mount shapes (convergence bug c). Each test pins one
// NAMED DECISION from workspaceMount.js; losing any of them silently changes
// the console's converse retry, the operator's loader binding, or re-opens
// the two-drifting-call-sites defect the module retires.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'

vi.mock('../api.js', () => ({
  getDrawingIntake: vi.fn(),
  getDrawingVersions: vi.fn(),
  redoDrawing: vi.fn(),
  undoDrawing: vi.fn(),
}))

import {
  getDrawingIntake,
  getDrawingVersions,
  redoDrawing,
  undoDrawing,
} from '../api.js'
import {
  CONSOLE_CONVERSE_DRAWING_ID,
  consoleWorkspaceMount,
  operatorWorkspaceMount,
} from './workspaceMount.js'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('console mount', () => {
  it('attaches the default console source with the not_found retry and NO drawing options', () => {
    const mount = consoleWorkspaceMount()
    expect(mount.drawingId).toBe(CONSOLE_CONVERSE_DRAWING_ID)
    expect(mount.retryNotFound).toBe(true)
    expect(mount.drawingOptions).toEqual({})
  })

  it('is referentially stable across calls (provider callback identity depends on it)', () => {
    expect(consoleWorkspaceMount()).toBe(consoleWorkspaceMount())
    expect(Object.isFrozen(consoleWorkspaceMount().drawingOptions)).toBe(true)
  })
})

describe('operator mount', () => {
  it('never retries not_found and binds every loader to the public-demo flag', () => {
    const mount = operatorWorkspaceMount({ publicDemo: true })
    expect(mount.retryNotFound).toBe(false)
    mount.drawingOptions.loadHead('d1')
    expect(getDrawingIntake).toHaveBeenCalledWith(true, 'd1', 'head')
    mount.drawingOptions.loadVersion('d1', 'v3')
    expect(getDrawingIntake).toHaveBeenCalledWith(true, 'd1', 'v3')
    // Slice 6a: the options object is FORWARDED, not dropped. /try renders the
    // same VersionList primitive /app's drawer does, delta chips included, and
    // the controller asks for them; this adapter used to swallow the flag.
    mount.drawingOptions.loadVersions('d1', { includeDeltas: true })
    expect(getDrawingVersions).toHaveBeenCalledWith(true, 'd1', { includeDeltas: true })
    mount.drawingOptions.undoVersion('d1', 'cap')
    expect(undoDrawing).toHaveBeenCalledWith(true, 'd1', 'cap')
    mount.drawingOptions.redoVersion('d1', 'cap')
    expect(redoDrawing).toHaveBeenCalledWith(true, 'd1', 'cap')
  })

  it('defaults the demo flag off and passes the stage callbacks through', () => {
    const onApplyIntake = vi.fn()
    const onResetSelection = vi.fn()
    const mount = operatorWorkspaceMount({ onApplyIntake, onResetSelection })
    mount.drawingOptions.loadVersions('d2', { includeDeltas: true })
    expect(getDrawingVersions).toHaveBeenCalledWith(false, 'd2', { includeDeltas: true })
    expect(mount.drawingOptions.onApplyIntake).toBe(onApplyIntake)
    expect(mount.drawingOptions.onResetSelection).toBe(onResetSelection)
  })
})

// Both call sites must ROUTE THROUGH the factory, or the shapes above pin
// nothing. Normalized to LF (Windows checkout is CRLF).
describe('call-site wiring', () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8').replace(/\r\n/g, '\n')

  it('SiteRoot mounts the console shape from the factory, not literals', () => {
    const src = read('../site/SiteRoot.jsx')
    expect(src).toMatch(/<WorkspaceControllerProvider \{\.\.\.consoleWorkspaceMount\(\)\}>/)
    expect(src).not.toMatch(/drawingId="rooftop_demo"/)
  })

  it('StageScene mounts the operator shape from the factory, not inline loaders', () => {
    const src = read('../site/StageScene.jsx')
    expect(src).toMatch(/operatorWorkspaceMount\(\{/)
    expect(src).toMatch(/drawingOptions=\{mount\.drawingOptions\}/)
    expect(src).toMatch(/retryNotFound=\{mount\.retryNotFound\}/)
    expect(src).not.toMatch(/loadHead:/)
  })
})
