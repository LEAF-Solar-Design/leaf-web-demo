import { describe, expect, it } from 'vitest'
import { versionsAvailable } from './App.jsx'
import { versionCluster } from './lib/ribbonClusters.js'
import { REASONS } from './lib/actionRegistry.js'

// The Undo version button as the toolbar builds it, fed by the App's own version flag.
const version = { version_id: 'v2', drawing_id: 'd1', seq: 2 }
function undo(flagInput, cluster = {}) {
  const tools = versionCluster({ hasVersions: versionsAvailable(flagInput), canUndo: true, ...cluster }).tools
  return tools.find((tool) => tool.id === 'undo')
}

describe('version buttons in a project', () => {
  it('Undo version is available for a selected project version with no standalone drawing', () => {
    const tool = undo({ projectMode: true, projectVersion: version, drawingState: null })
    expect(tool.disabled).toBeFalsy()
    expect(tool.reason || '').not.toBe(REASONS.noVersions)
  })

  it('a project with no selected version says there are no saved versions, whatever is standalone', () => {
    const tool = undo({ projectMode: true, projectVersion: undefined, drawingState: { drawing_id: 'demo', head: 3 } })
    expect(tool.disabled).toBe(true)
    expect(tool.reason).toBe(REASONS.noVersions)
  })

  it('the later gates still apply in a project', () => {
    const flag = { projectMode: true, projectVersion: version, drawingState: null }
    expect(undo(flag, { canUndo: false }).disabled).toBe(true)
    expect(undo(flag, { canUndo: false }).reason).not.toBe(REASONS.noVersions)
    expect(undo(flag, { versionBusy: true }).reason).toBe(REASONS.versionBusy)
    expect(undo(flag, { running: true }).disabled).toBe(true)
  })

  it('outside a project the flag is the standalone drawing state, as before', () => {
    expect(versionsAvailable({ projectMode: false, projectVersion: version, drawingState: null })).toBe(false)
    expect(versionsAvailable({ projectMode: false, projectVersion: undefined, drawingState: { head: 1 } })).toBe(true)
    expect(undo({ projectMode: false, projectVersion: version, drawingState: null }).reason).toBe(REASONS.noVersions)
    expect(undo({ projectMode: false, projectVersion: undefined, drawingState: { head: 1 } }).disabled).toBeFalsy()
  })
})
