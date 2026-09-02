// @vitest-environment node
//
// The ribbon's clusters as data (W4d Slice A): every group is a real
// command with honest gating — a disabled tool always carries its reason,
// an unavailable group its note, and no cluster is ever fabricated.
import { describe, expect, it, vi } from 'vitest'

import {
  MAX_LAYER_TOOLS,
  REASONS,
  RIBBON_RATIONALE,
  authorCluster,
  catalogClusters,
  layersCluster,
  versionCluster,
  viewCluster,
} from './ribbonClusters.js'

const FAMS = [
  {
    family_id: 'measurement',
    label: 'Measurement',
    capabilities: [
      { name: 'count-by-layer', description: 'Counts entities per layer.', capabilities: ['drawing.read'] },
    ],
  },
  {
    family_id: 'custom',
    label: 'Custom authored tools',
    capabilities: [
      { name: 'delete-marked-panel', description: 'Deletes the marked panel.', capabilities: ['drawing.write'] },
    ],
  },
]

function toolsOf(cluster) {
  return Object.fromEntries(cluster.tools.map((t) => [t.id, t]))
}

describe('catalogClusters', () => {
  it('maps one family cluster per family and arms the catalog run path with ribbon attribution', () => {
    const onRequestRun = vi.fn()
    const clusters = catalogClusters(FAMS, { onRequestRun })
    expect(clusters.map((c) => [c.id, c.kind])).toEqual([['measurement', 'family'], ['custom', 'family']])
    const read = toolsOf(clusters[0])['count-by-layer']
    expect(read.disabled).toBe(false)
    expect(read.write).toBe(false)
    read.onClick()
    expect(onRequestRun).toHaveBeenCalledWith(FAMS[0].capabilities[0], null, RIBBON_RATIONALE, 'ribbon')
  })

  it('disables write tools under the single-writer lock with the reason, and leaves read tools live', () => {
    const clusters = catalogClusters(FAMS, { onRequestRun: () => {}, writeLocked: true })
    const write = toolsOf(clusters[1])['delete-marked-panel']
    expect(write.disabled).toBe(true)
    expect(write.reason).toBe(REASONS.writeLocked)
    expect(toolsOf(clusters[0])['count-by-layer'].disabled).toBe(false)
  })

  it('prefers the caller-supplied lock note and names the plan when unentitled', () => {
    const locked = catalogClusters(FAMS, { onRequestRun: () => {}, writeLocked: true, writeLockNote: 'held by ops' })
    expect(toolsOf(locked[1])['delete-marked-panel'].reason).toBe('held by ops')
    const unentitled = catalogClusters(FAMS, { onRequestRun: () => {}, writeEntitled: false })
    expect(toolsOf(unentitled[1])['delete-marked-panel'].reason).toBe(REASONS.writeUnentitled)
  })

  it('disables everything while a run is in flight', () => {
    const clusters = catalogClusters(FAMS, { onRequestRun: () => {}, running: true })
    expect(clusters.flatMap((c) => c.tools).every((t) => t.disabled)).toBe(true)
  })

  it('an empty fold is ONE honest cluster with the sentence and zero tools', () => {
    for (const families of [[], null, undefined]) {
      const clusters = catalogClusters(families, { onRequestRun: () => {} })
      expect(clusters).toHaveLength(1)
      expect(clusters[0].tools).toEqual([])
      expect(clusters[0].note).toBe('No tools for this surface yet.')
    }
  })
})

describe('viewCluster', () => {
  it('drives the viewer ref surface and is disabled with a reason without a drawing', () => {
    const viewer = { setView: vi.fn(() => true), getPose: () => ({ zoom: 2 }) }
    const on = viewCluster({ viewerRef: { current: viewer }, hasDrawing: true })
    toolsOf(on).fit.onClick()
    expect(viewer.setView).toHaveBeenCalledWith('home')
    toolsOf(on)['zoom-in'].onClick()
    expect(viewer.setView).toHaveBeenLastCalledWith({ zoom: 2.5 })
    const off = viewCluster({ viewerRef: { current: null }, hasDrawing: false })
    expect(off.tools.every((t) => t.disabled && t.reason === REASONS.noDrawing)).toBe(true)
    // A missing viewer is a no-op, never a throw.
    expect(() => toolsOf(off).fit.onClick()).not.toThrow()
  })
})

describe('versionCluster', () => {
  it('gates undo/redo/history exactly like the toolbar, each with its reason', () => {
    const onUndo = vi.fn(); const onRedo = vi.fn(); const onToggleHistory = vi.fn()
    const live = toolsOf(versionCluster({ hasVersions: true, canUndo: true, canRedo: false, onUndo, onRedo, onToggleHistory, historyOpen: true }))
    expect(live.undo.disabled).toBe(false)
    live.undo.onClick(); expect(onUndo).toHaveBeenCalledTimes(1)
    expect(live.redo.disabled).toBe(true)
    expect(live.redo.reason).toBe(REASONS.nothingToRedo)
    expect(live.history.expanded).toBe(true)
    live.history.onClick(); expect(onToggleHistory).toHaveBeenCalledTimes(1)
  })

  it('the shared reasons win in resolution order: no versions, busy, running, preview, blocked', () => {
    const base = { hasVersions: true, canUndo: true, canRedo: true }
    expect(toolsOf(versionCluster({ ...base, hasVersions: false })).undo.reason).toBe(REASONS.noVersions)
    expect(toolsOf(versionCluster({ ...base, versionBusy: true })).undo.reason).toBe(REASONS.versionBusy)
    expect(toolsOf(versionCluster({ ...base, running: true })).redo.reason).toBe(REASONS.running)
    expect(toolsOf(versionCluster({ ...base, previewing: true })).undo.reason).toBe(REASONS.previewing)
    expect(toolsOf(versionCluster({ ...base, mutationsBlocked: true })).undo.reason).toBe(REASONS.mutationsBlocked)
    // History ignores the mutation gates: reading the chain is never a write.
    expect(toolsOf(versionCluster({ ...base, previewing: true, mutationsBlocked: true })).history.disabled).toBe(false)
  })
})

describe('layersCluster', () => {
  it('one pressed toggle per layer, visible unless the map says false, calling the toggle by name', () => {
    const onToggle = vi.fn()
    const cluster = layersCluster({ layers: ['Panels', 'Roof'], counts: { Panels: 3 }, visibleLayers: { Roof: false }, onToggle })
    expect(cluster.tools.map((t) => [t.label, t.pressed])).toEqual([['Panels', true], ['Roof', false]])
    expect(cluster.tools[0].title).toBe('Hide Panels (3)')
    expect(cluster.tools[1].title).toBe('Show Roof (0)')
    cluster.tools[1].onClick()
    expect(onToggle).toHaveBeenCalledWith('Roof')
    expect(cluster.note).toBeNull()
  })

  it('bounds the strip and names the remainder; no layers is an honest note', () => {
    const layers = Array.from({ length: MAX_LAYER_TOOLS + 4 }, (_, i) => `L${i}`)
    const cluster = layersCluster({ layers, onToggle: () => {} })
    expect(cluster.tools).toHaveLength(MAX_LAYER_TOOLS)
    expect(cluster.note).toBe('+4 more in the Layers palette')
    const empty = layersCluster({ layers: undefined })
    expect(empty.tools).toEqual([])
    expect(empty.note).toBe(REASONS.noDrawing)
  })
})

describe('authorCluster', () => {
  it('one real command, disabled with the plan reason when authoring is not entitled', () => {
    const onOpen = vi.fn()
    const on = authorCluster({ onOpen, entitled: true })
    on.tools[0].onClick()
    expect(onOpen).toHaveBeenCalledTimes(1)
    const off = authorCluster({ onOpen, entitled: false })
    expect(off.tools[0].disabled).toBe(true)
    expect(off.tools[0].reason).toBe(REASONS.buildUnentitled)
  })
})
