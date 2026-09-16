// @vitest-environment node
//
// The ribbon's clusters as data (W4d Slice A): every group is a real
// command with honest gating — a disabled tool always carries its reason,
// an unavailable group its note, and no cluster is ever fabricated.
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'

import { DEFERRED_REASONS } from './actionRegistry.js'
import { RIBBON_TABS } from '../site/CockpitTopBand.jsx'
import { hasIcon } from '../site/CockpitIcon.jsx'
import {
  CATALOG_TOOL_NOTE_ALL_PLACED,
  MAX_LAYER_TOOLS,
  PROFILE_REASONS,
  REASONS,
  RIBBON_RATIONALE,
  authorCluster,
  catalogClusters,
  catalogTabClusters,
  layersCluster,
  profileRibbonTabs,
  solarRouteDisplay,
  railCluster,
  referencePanels,
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

describe('profile command icons', () => {
  it('uses installed icons for every project and ship command while retaining reasons', () => {
    for (const profile of ['project', 'ship']) {
      const tools = profileRibbonTabs(profile).flatMap((tab) => tab.clusters.flatMap((cluster) => cluster.tools))
      expect(tools.length).toBeGreaterThan(0)
      for (const tool of tools) {
        expect(hasIcon(tool.icon), tool.id).toBe(true)
        expect(tool.icon, tool.id).not.toBe('toolbox')
        expect(tool.disabled, tool.id).toBe(true)
        expect(typeof tool.reason, tool.id).toBe('string')
        expect(tool.reason.length, tool.id).toBeGreaterThan(0)
      }
    }
  })
})

function toolsOf(cluster) {
  return Object.fromEntries(cluster.tools.map((t) => [t.id, t]))
}

describe('row9 Solar solved-route eligibility', () => {
  const bundle = JSON.parse(readFileSync(new URL('../../public/demo-solve.json', import.meta.url), 'utf8'))
  const routes = bundle.solve.strings.filter((route) => Array.isArray(route.pts) && route.pts.length >= 2)
  const clean = { eligible: true, head: 1, previewing: false, engineDirty: false, shown: true, routes }
  it('row7 shows the 134 drawable solved rooftop routes by default', () => {
    expect(solarRouteDisplay(clean)).toHaveLength(134)
    const onToggle = vi.fn()
    const toggle = profileRibbonTabs('solar', { solar: { eligible: true, onToggle } })[1].clusters[1].tools[0]
    expect(toggle).toMatchObject({ pressed: true, disabled: false })
    toggle.onClick()
    expect(onToggle).toHaveBeenCalledOnce()
  })
  it('row8 turning the toggle off clears the overlay', () => {
    expect(solarRouteDisplay({ ...clean, shown: false })).toBeUndefined()
    expect(profileRibbonTabs('solar', { solar: { eligible: true, shown: false, onToggle: vi.fn() } })[1].clusters[1].tools[0].pressed).toBe(false)
  })
  it.each([
    ['live tenant', { eligible: false }], ['edit fixture', { eligible: false }],
    ['version preview', { previewing: true }], ['mutated head', { head: 2 }], ['dirty engine', { engineDirty: true }],
  ])('row9 %s has no routes and names the unavailable state', (_, gate) => {
    expect(solarRouteDisplay({ ...clean, ...gate })).toBeUndefined()
    const toggle = profileRibbonTabs('solar', { solar: { eligible: false, onToggle: vi.fn() } })[1].clusters[1].tools[0]
    expect(toggle).toMatchObject({ disabled: true, pressed: false, reason: 'Solved routes are available only for the unchanged rooftop demo' })
  })
})

describe('row11 Solar catalog gate parity', () => {
  it.each([
    ['running', { running: true }], ['preview', { previewing: true }],
    ['write lock', { writeLocked: true }], ['entitlement', { writeEntitled: false }],
    ['dirty engine', { engineDirty: true }], ['unwired MCP', {}],
  ])('row11 retains the Draw catalog reason for %s', (name, gate) => {
    for (const family_id of ['stringing', 'placement', 'measurement', 'selection']) {
      const capability = { name: 'catalog-member', capabilities: ['drawing.write'],
        ...(name === 'unwired MCP' ? { mcp_source: { server_id: 'abcdef0123456789abcdef01', tool: 'list-items' } } : {}) }
      const family = { family_id, label: family_id, capabilities: [capability] }
      const draw = catalogClusters([family], gate)[0].tools[0]
      const solar = profileRibbonTabs('solar', { families: [family], catalogOptions: gate })[1].clusters
        .find((cluster) => cluster.id === family_id).tools.find((tool) => tool.id === capability.name)
      expect(solar.disabled).toBe(true)
      expect(solar.reason).toBe(draw.reason)
      expect(solar.reason.length).toBeGreaterThan(0)
    }
  })
  it('clears the console selection only when one exists', () => {
    const onClearSelection = vi.fn()
    const clear = profileRibbonTabs('solar', { selectedHandle: '9', onClearSelection })[1].clusters[4].tools.at(-1)
    expect(clear.disabled).toBe(false)
    clear.onClick()
    expect(onClearSelection).toHaveBeenCalledOnce()
  })
})

describe('profileRibbonTabs', () => {
  it('keeps the drafting tab order and reasons, with caller-owned clusters', () => {
    expect(profileRibbonTabs('drafting')).toEqual(RIBBON_TABS.map((tab) => ({ ...tab, clusters: [] })))
    for (const profile of ['unknown', null, undefined, {}, ['solar'], 7]) {
      expect(profileRibbonTabs(profile)).toEqual(profileRibbonTabs('drafting'))
    }
  })

  it('row10 inserts Solar after Draw with four honest empty families and local seats', () => {
    for (const families of [undefined, [], [{ family_id: 'stringing', capabilities: [] }, { id: 'placement', capabilities: [] }]]) {
      const tabs = profileRibbonTabs('solar', { families })
      expect(tabs.map((tab) => tab.id)).toEqual(['draw', 'solar', 'model', 'insert', 'annotate', 'view', 'manage'])
      expect(tabs.filter((tab) => tab.id !== 'solar')).toEqual(profileRibbonTabs('drafting'))
      expect(tabs[1].clusters.map(({ id, label, kind }) => ({ id, label, kind }))).toEqual([
        { id: 'solar-panels', label: 'Panel placement', kind: 'group' },
        { id: 'stringing', label: 'Stringing', kind: 'group' },
        { id: 'placement', label: 'Equipment placement', kind: 'group' },
        { id: 'measurement', label: 'Measure', kind: 'group' },
        { id: 'selection', label: 'Select', kind: 'group' },
      ])
      expect(tabs[1].clusters[0].tools).toEqual([]) // Filled by the engine consumer.
      for (const cluster of tabs[1].clusters.slice(1)) {
        const empty = cluster.tools.find((tool) => tool.id === `${cluster.id}:empty`)
        expect(empty).toMatchObject({ disabled: true, reason: `No ${cluster.id} tools in this catalog yet` })
        expect(empty.onClick).toBeUndefined()
      }
      expect(tabs[1].clusters[1].tools[0].label).toBe('Show solved rooftop strings')
      expect(tabs[1].clusters[4].tools.at(-1)).toMatchObject({ label: 'Clear selection', disabled: true })
    }
  })

  it('uses the catalog tool projection and gates for real solar families', () => {
    const onRun = vi.fn()
    const read = { name: 'string-panels', label: 'String panels', icon: 'layers', description: 'Read panel strings.', placement: { tab: 'draw', size: 'row' } }
    const write = { name: 'place-inverter', capabilities: ['drawing.write'] }
    const families = [
      { family_id: 'stringing', label: 'Strings', capabilities: [read] },
      { id: 'placement', label: 'Placement', capabilities: [write] },
    ]
    const ctx = { families, onRun, catalogOptions: { writeLocked: true, writeLockNote: 'Held by another editor' } }
    const [, stringing, placement] = profileRibbonTabs('solar', ctx)[1].clusters
    expect(stringing.tools[1]).toMatchObject({ id: read.name, label: read.name, text: read.label, icon: 'layers', size: 'row', title: read.description, disabled: false, reason: '' })
    stringing.tools[1].onClick()
    expect(onRun).toHaveBeenCalledTimes(1)
    expect(onRun).toHaveBeenCalledWith(read)
    expect(placement.tools[0]).toMatchObject({ disabled: true, write: true, reason: 'Held by another editor' })
    expect(profileRibbonTabs('solar', { ...ctx, catalogOptions: { running: true } })[1].clusters[1].tools[1].reason).toBe(REASONS.running)
    // The records are the catalog projection's own: a tool that names its own
    // tab still seats on Solar (the family is the home), and a write tool is
    // gated by the same entitlement rung with the same reason.
    const unentitled = profileRibbonTabs('solar', { families, onRun, catalogOptions: { writeEntitled: false } })[1].clusters
    expect(unentitled[1].tools[1]).toMatchObject({ id: read.name, disabled: false, reason: '' })
    expect(unentitled[2].tools[0]).toMatchObject({ id: write.name, write: true, disabled: true, reason: REASONS.writeUnentitled })
  })

  it('wires project, file, conversation, activity and catalog handlers', () => {
    const handlers = Array.from({ length: 8 }, () => vi.fn())
    const [onOpen, onChange, onCreate, onUpload, onNew, onJobs, onReceipts, onRequestRun] = handlers
    const tabs = profileRibbonTabs('project', {
      project: { onOpen, onChange, onCreate }, files: { onUpload }, conversation: { onNew },
      activity: { onJobs, onReceipts }, families: FAMS, catalogOptions: { onRequestRun },
    })
    expect(tabs.map(({ id, label }) => [id, label])).toEqual([['project', 'Project'], ['tools', 'Tools'], ['activity', 'Activity']])
    expect(tabs[0].clusters.map((cluster) => cluster.label)).toEqual(['Project', 'Files', 'Conversation'])
    expect(tabs[2].clusters.map((cluster) => cluster.label)).toEqual(['Jobs', 'Receipts'])
    const commands = [...tabs[0].clusters, ...tabs[2].clusters].flatMap((cluster) => cluster.tools)
    expect(commands.map((tool) => tool.label)).toEqual(['Open project', 'Change project', 'Create project', 'Upload drawing', 'New conversation', 'Open job monitor', 'Open receipts'])
    commands.forEach((tool) => { expect(tool.disabled).toBe(false); tool.onClick() })
    handlers.slice(0, 7).forEach((handler) => expect(handler).toHaveBeenCalledTimes(1))
    const withoutClicks = (clusters) => clusters.map((cluster) => ({ ...cluster, tools: cluster.tools.map(({ onClick, ...tool }) => tool) }))
    expect(withoutClicks(tabs[1].clusters)).toEqual(withoutClicks(catalogClusters(FAMS, { onRequestRun })))
    tabs[1].clusters[0].tools[0].onClick()
    expect(onRequestRun).toHaveBeenCalledWith(FAMS[0].capabilities[0], null, RIBBON_RATIONALE, 'ribbon')
  })

  it('disables missing or invalid project handlers with the fixed profile reasons', () => {
    const tabs = profileRibbonTabs('project', {
      project: { onOpen: null, onChange: false, onCreate: 'create' },
      files: {}, conversation: { onNew: 7 }, activity: { onJobs: {}, onReceipts: [] }, families: [],
    })
    const tools = [...tabs[0].clusters, ...tabs[2].clusters].flatMap((cluster) => cluster.tools)
    expect(tools.map((tool) => tool.reason)).toEqual([
      PROFILE_REASONS.openProject, PROFILE_REASONS.changeProject, PROFILE_REASONS.createProject,
      PROFILE_REASONS.uploadDrawing, PROFILE_REASONS.newConversation, PROFILE_REASONS.openJobs, PROFILE_REASONS.openReceipts,
    ])
    const expectedIcons = {
      'project:open': 'open',
      'project:change': 'open',
      'project:create': 'new-file',
      'files:upload': 'import',
      'conversation:new': 'leader',
      'activity:jobs': 'history',
      'activity:receipts': 'save',
    }
    for (const tool of tools) {
      expect(tool).toMatchObject({ disabled: true, icon: expectedIcons[tool.id], title: tool.label })
      expect(tool.onClick).toBeUndefined()
    }
    // Reason text is fixed per tool; ctx cannot override it.
    const overridden = profileRibbonTabs('project', { project: { onOpen: null, reasons: { open: 'Custom' } }, files: { reason: 'Custom' } })
    expect(overridden[0].clusters[0].tools[0].reason).toBe(PROFILE_REASONS.openProject)
    expect(overridden[0].clusters[1].tools[0].reason).toBe(PROFILE_REASONS.uploadDrawing)
    expect(overridden[1].clusters).toEqual(catalogClusters([]))
  })

  it('keeps ship statuses honest and enables only real launch and receipt handlers', () => {
    const onLaunch = vi.fn()
    const onReceipts = vi.fn()
    const [tab] = profileRibbonTabs('ship', { ship: { onLaunch, onReceipts } })
    expect([tab.id, tab.label]).toEqual(['ship', 'Ship'])
    expect(tab.clusters.map((cluster) => cluster.label)).toEqual(['Revision', 'Readiness', 'Ship', 'Receipts'])
    const tools = tab.clusters.flatMap((cluster) => cluster.tools)
    expect(tools.map((tool) => tool.label)).toEqual(['Approved revision', 'Mounted Apple readiness', 'TestFlight build', 'Open ship receipts'])
    // The two status rows have no handler to bind yet: disabled, and they say so.
    expect(tools.slice(0, 2).map((tool) => [tool.disabled, tool.reason, tool.onClick])).toEqual([
      [true, PROFILE_REASONS.approvedRevision, undefined], [true, PROFILE_REASONS.appleReadiness, undefined],
    ])
    expect(tools[2].disabled).toBe(false)
    tools[2].onClick()
    expect(onLaunch).toHaveBeenCalledTimes(1)
    tools[3].onClick()
    expect(onReceipts).toHaveBeenCalledTimes(1)
    // No launch handler: no launch path is implied.
    const [, , launch, receipts] = profileRibbonTabs('ship', { ship: { onLaunch: 'go', launchReason: 'ignored' } })[0].clusters.map((cluster) => cluster.tools[0])
    expect(launch).toMatchObject({ disabled: true, reason: PROFILE_REASONS.testflightBuild })
    expect(launch.onClick).toBeUndefined()
    expect(receipts).toMatchObject({ disabled: true, reason: PROFILE_REASONS.shipReceipts })
    expect(receipts.onClick).toBeUndefined()
  })

  it('freezes one plain sentence per profile tool', () => {
    expect(Object.isFrozen(PROFILE_REASONS)).toBe(true)
    for (const sentence of Object.values(PROFILE_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
    }
  })

  it('handles absent and malformed context without inventing enabled commands', () => {
    for (const ctx of [undefined, null, false, 42, 'context', [], {
      project: null, files: [], conversation: false, activity: 'jobs', ship: { onLaunch: true, launchReason: {} },
      catalogOptions: { onRequestRun: 'run', onOpenFamily: true, writeLockNote: {} },
      families: [null, false, [], { id: 'stringing', capabilities: {} }, { id: 'placement', capabilities: [null, {}, false] }],
    }]) {
      for (const profile of ['drafting', 'solar', 'project', 'ship']) {
        const tabs = profileRibbonTabs(profile, ctx)
        for (const tool of tabs.flatMap((tab) => tab.clusters.flatMap((cluster) => cluster.tools))) {
          expect(tool.disabled).toBe(true)
          expect(typeof tool.reason).toBe('string')
          expect(tool.reason.length).toBeGreaterThan(0)
          expect(tool.onClick).toBeUndefined()
        }
      }
    }
  })
})

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

  it('W4g-2: unsaved browser-engine edits disable write tools with the reason; read tools stay live; the lock outranks it', () => {
    const dirty = catalogClusters(FAMS, { onRequestRun: () => {}, engineDirty: true })
    const write = toolsOf(dirty[1])['delete-marked-panel']
    expect(write.disabled).toBe(true)
    expect(write.reason).toBe(REASONS.unsavedEngineEdits)
    expect(toolsOf(dirty[0])['count-by-layer'].disabled).toBe(false)
    const both = catalogClusters(FAMS, { onRequestRun: () => {}, engineDirty: true, writeLocked: true })
    expect(toolsOf(both[1])['delete-marked-panel'].reason).toBe(REASONS.writeLocked)
    const clean = catalogClusters(FAMS, { onRequestRun: () => {}, engineDirty: false })
    expect(toolsOf(clean[1])['delete-marked-panel'].disabled).toBe(false)
  })

  it('prefers the caller-supplied lock note and names the plan when unentitled', () => {
    const locked = catalogClusters(FAMS, { onRequestRun: () => {}, writeLocked: true, writeLockNote: 'held by ops' })
    expect(toolsOf(locked[1])['delete-marked-panel'].reason).toBe('held by ops')
    const unentitled = catalogClusters(FAMS, { onRequestRun: () => {}, writeEntitled: false })
    expect(toolsOf(unentitled[1])['delete-marked-panel'].reason).toBe(REASONS.writeUnentitled)
  })

  it('disables everything during a run or preview and names the distinct reason', () => {
    const running = catalogClusters(FAMS, { onRequestRun: () => {}, running: true })
    expect(running.flatMap((c) => c.tools).every((t) => t.disabled && t.reason === REASONS.running)).toBe(true)
    const previewing = catalogClusters(FAMS, { onRequestRun: () => {}, previewing: true })
    expect(previewing.flatMap((c) => c.tools).every((t) => t.disabled && t.reason === REASONS.previewing)).toBe(true)
  })

  it('an empty fold is ONE honest cluster with the sentence and zero tools', () => {
    for (const families of [[], null, undefined]) {
      const clusters = catalogClusters(families, { onRequestRun: () => {} })
      expect(clusters).toHaveLength(1)
      expect(clusters[0].tools).toEqual([])
      expect(clusters[0].note).toBe('No tools for this surface yet.')
    }
  })

  // Standardization slice 8c. Nothing is projected onto any real catalog yet
  // (server/mcp_tool_projection.py always returns []), so this is a fixture:
  // no live surface can produce this record today, but the honesty path that
  // will disable it once one does is real and pinned here, not left to be
  // discovered the day the projection stops being empty.
  it('an mcp-sourced tool cannot run on this surface yet, and says so', () => {
    const mcpFams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [{
        name: 'list-linked-service-items',
        description: 'Lists items from a connected service.',
        capabilities: ['drawing.read'],
        mcp_source: { server_id: 'abcdef0123456789abcdef01', tool: 'list-items' },
      }],
    }]
    const clusters = catalogClusters(mcpFams, { onRequestRun: () => {} })
    const tool = toolsOf(clusters[0])['list-linked-service-items']
    expect(tool.disabled).toBe(true)
    expect(tool.reason).toBe(REASONS.mcpToolNotWired)
    expect(tool.mcpSource).toEqual({ server_id: 'abcdef0123456789abcdef01', tool: 'list-items' })
  })

  it('a hostile mcp_source is dropped, so the tool runs like an ordinary catalog row', () => {
    const badFams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [{
        name: 'count-by-layer',
        description: 'Counts entities per layer.',
        capabilities: ['drawing.read'],
        mcp_source: { server_id: 'not-hex-shaped', tool: 'list-items' },
      }],
    }]
    const clusters = catalogClusters(badFams, { onRequestRun: () => {} })
    const tool = toolsOf(clusters[0])['count-by-layer']
    expect(tool.disabled).toBe(false)
    expect(tool.mcpSource).toBeUndefined()
  })
})

describe('the rail affordances the band carries while the rail is hidden', () => {
  it('a family cluster label opens that family when a handler is given, and is decoration otherwise', () => {
    const onOpenFamily = vi.fn()
    const [cluster] = catalogClusters(FAMS, { onRequestRun: () => {}, onOpenFamily })
    expect(cluster.labelTitle).toBe('Open Measurement in the tool rail (1 tools)')
    cluster.onLabelClick()
    expect(onOpenFamily).toHaveBeenCalledWith(FAMS[0])
    const [plain] = catalogClusters(FAMS, { onRequestRun: () => {} })
    expect(plain.onLabelClick).toBeNull()
  })

  it('railCluster is one real command: expand the hidden rail', () => {
    const onExpand = vi.fn()
    const cluster = railCluster({ onExpand })
    expect(cluster.tools.map((t) => t.id)).toEqual(['rail-expand'])
    cluster.tools[0].onClick()
    expect(onExpand).toHaveBeenCalledTimes(1)
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

  it('carries the Properties pane toggle only when the caller owns the pane, as a pressed-state tool', () => {
    expect(toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: true }))['properties-pane']).toBeUndefined()
    const onTogglePane = vi.fn()
    const open = toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: false, paneOpen: true, onTogglePane }))['properties-pane']
    expect(open.pressed).toBe(true)
    expect(open.disabled).toBeUndefined()
    open.onClick()
    expect(onTogglePane).toHaveBeenCalledTimes(1)
    const closed = toolsOf(viewCluster({ viewerRef: { current: null }, hasDrawing: true, paneOpen: false, onTogglePane }))['properties-pane']
    expect(closed.pressed).toBe(false)
    expect(closed.title).toMatch(/Open/)
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

describe('the tool record carries icon, size and tab (slice 3)', () => {
  // The regression this whole slice must not cause: a catalog whose tools
  // declare NOTHING renders byte-identically to before the record grew fields.
  it('is byte-identical for tools that declare no icon and no placement', () => {
    const onRequestRun = () => {}
    const clusters = catalogClusters(FAMS, { onRequestRun })
    expect(clusters.map((c) => ({
      id: c.id,
      label: c.label,
      kind: c.kind,
      tools: c.tools.map(({ onClick, ...rest }) => rest),
    }))).toEqual([
      {
        id: 'measurement',
        label: 'Measurement',
        kind: 'family',
        tools: [{
          id: 'count-by-layer',
          label: 'count-by-layer',
          text: 'count-by-layer',
          icon: 'toolbox',
          size: 'large',
          title: 'Counts entities per layer.',
          write: false,
          disabled: false,
          reason: '',
        }],
      },
      {
        id: 'custom',
        label: 'Custom authored tools',
        kind: 'family',
        tools: [{
          id: 'delete-marked-panel',
          label: 'delete-marked-panel',
          text: 'delete-marked-panel',
          icon: 'toolbox',
          size: 'large',
          title: 'Deletes the marked panel.',
          write: true,
          disabled: false,
          reason: '',
        }],
      },
    ])
    expect(catalogTabClusters(FAMS, { onRequestRun })).toEqual({})
  })

  it('reads the icon and the size off the record when it declares them', () => {
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'count-by-layer', icon: 'layers', placement: { size: 'small' }, capabilities: [] },
        { name: 'measure-panel-area', capabilities: [] },
      ],
    }]
    const tools = toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])
    expect([tools['count-by-layer'].icon, tools['count-by-layer'].size]).toEqual(['layers', 'small'])
    expect([tools['measure-panel-area'].icon, tools['measure-panel-area'].size]).toEqual(['toolbox', 'large'])
  })

  it('ignores an unknown size rather than rendering an unknown ribbon shape', () => {
    const fams = [{
      family_id: 'measurement', label: 'Measurement',
      capabilities: [{ name: 'count-by-layer', placement: { size: 'huge' }, capabilities: [] }],
    }]
    expect(toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])['count-by-layer'].size).toBe('large')
  })

  it('moves a tool that names a tab OUT of the families panel and into that tab', () => {
    const onRequestRun = vi.fn()
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'count-by-layer', placement: { tab: 'annotate' }, capabilities: [] },
        { name: 'measure-panel-area', capabilities: [] },
      ],
    }]
    const stay = catalogClusters(fams, { onRequestRun })
    expect(stay.map((c) => c.id)).toEqual(['measurement'])
    expect(Object.keys(toolsOf(stay[0]))).toEqual(['measure-panel-area'])

    const byTab = catalogTabClusters(fams, { onRequestRun })
    expect(Object.keys(byTab)).toEqual(['annotate'])
    expect(byTab.annotate.map((c) => [c.id, c.label])).toEqual([['measurement@annotate', 'Measurement']])
    const placed = toolsOf(byTab.annotate[0])['count-by-layer']
    placed.onClick()
    expect(onRequestRun).toHaveBeenCalledWith(
      fams[0].capabilities[0], null, RIBBON_RATIONALE, 'ribbon')
  })

  it('seats one family on two tabs as two distinct clusters', () => {
    const fams = [{
      family_id: 'measurement',
      label: 'Measurement',
      capabilities: [
        { name: 'a', placement: { tab: 'draw' }, capabilities: [] },
        { name: 'b', placement: { tab: 'view' }, capabilities: [] },
      ],
    }]
    const byTab = catalogTabClusters(fams, { onRequestRun: () => {} })
    expect(byTab.draw[0].id).toBe('measurement@draw')
    expect(byTab.view[0].id).toBe('measurement@view')
    // Every tool moved, so the families panel keeps no empty shell.
    expect(catalogClusters(fams, { onRequestRun: () => {} })).toEqual([
      { id: 'tools', label: 'Tools', kind: 'group', note: CATALOG_TOOL_NOTE_ALL_PLACED, tools: [] },
    ])
  })

  it('an unknown tab leaves the tool exactly where it renders today', () => {
    const fams = [{
      family_id: 'measurement', label: 'Measurement',
      capabilities: [{ name: 'count-by-layer', placement: { tab: 'model' }, capabilities: [] }],
    }]
    expect(Object.keys(toolsOf(catalogClusters(fams, { onRequestRun: () => {} })[0])))
      .toEqual(['count-by-layer'])
    expect(catalogTabClusters(fams, { onRequestRun: () => {} })).toEqual({})
  })

  it('carries the write gating onto a placed tool too', () => {
    const fams = [{
      family_id: 'custom', label: 'Custom authored tools',
      capabilities: [{
        name: 'delete-marked-panel', placement: { tab: 'draw' }, capabilities: ['drawing.write'],
      }],
    }]
    const byTab = catalogTabClusters(fams, { onRequestRun: () => {}, writeLocked: true })
    const tool = toolsOf(byTab.draw[0])['delete-marked-panel']
    expect([tool.write, tool.disabled, tool.reason]).toEqual([true, true, REASONS.writeLocked])
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
    // Entitled but the authoring stage is off: a different fix, a different sentence.
    const dark = authorCluster({ onOpen, entitled: true, available: false })
    expect(dark.tools[0].disabled).toBe(true)
    expect(dark.tools[0].reason).toBe(REASONS.buildUnavailable)
    // The plan's answer outranks availability when both are false.
    expect(authorCluster({ onOpen, entitled: false, available: false }).tools[0].reason).toBe(REASONS.buildUnentitled)
  })

  it('carries no second tool until something has been published', () => {
    expect(authorCluster({ onOpen: () => {} }).tools.map((t) => t.id)).toEqual(['author-tool'])
    expect(authorCluster({ onOpen: () => {}, authored: null }).tools).toHaveLength(1)
  })

  it('shows a just-published tool with the record icon and runs it through onUseAuthored', () => {
    const onUseAuthored = vi.fn()
    const authored = { name: 'panel-audit', icon: 'layers', catalog_digest: 'sha256:abc' }
    const cluster = authorCluster({ onOpen: () => {}, authored, onUseAuthored })
    const tool = cluster.tools[1]
    expect([tool.id, tool.label, tool.icon, tool.size, tool.disabled, tool.reason])
      .toEqual(['authored:panel-audit', 'panel-audit', 'layers', 'large', false, ''])
    tool.onClick()
    expect(onUseAuthored).toHaveBeenCalledWith(authored)
  })

  it('falls back to the shared toolbox glyph when the published record names no icon', () => {
    const cluster = authorCluster({
      onOpen: () => {}, authored: { name: 'panel-audit', catalog_digest: 'sha256:abc' },
    })
    expect(cluster.tools[1].icon).toBe('toolbox')
  })

  it('says honestly that a digest-less publish is not runnable yet', () => {
    // Exact string, pinned: this is the sentence a user reads, and it is the
    // ribbon's half of publishedCatalogTool.js's fail-closed rule.
    expect(REASONS.publishing).toBe('publishing: not in the runnable catalog yet')
    for (const authored of [
      { name: 'panel-audit' },
      { name: 'panel-audit', catalog_digest: '' },
      { name: 'panel-audit', catalog_digest: 7 },
    ]) {
      const tool = authorCluster({ onOpen: () => {}, authored }).tools[1]
      expect(tool.disabled).toBe(true)
      expect(tool.reason).toBe(REASONS.publishing)
    }
  })

  it('a settled authored tool still yields to a run in flight or a version preview', () => {
    const authored = { name: 'panel-audit', catalog_digest: 'sha256:abc' }
    expect(authorCluster({ onOpen: () => {}, authored, running: true }).tools[1].reason)
      .toBe(REASONS.running)
    expect(authorCluster({ onOpen: () => {}, authored, previewing: true }).tools[1].reason)
      .toBe(REASONS.previewing)
  })

  it('carries the write gating onto an authored write tool, mirroring familyCluster (3c)', () => {
    const authored = { name: 'delete-marked-panel', catalog_digest: 'sha256:abc', capabilities: ['drawing.write'] }
    const locked = authorCluster({ onOpen: () => {}, authored, writeLocked: true })
    expect(locked.tools[1].disabled).toBe(true)
    expect(locked.tools[1].reason).toBe(REASONS.writeLocked)
    const noted = authorCluster({ onOpen: () => {}, authored, writeLocked: true, writeLockNote: 'held by ops' })
    expect(noted.tools[1].reason).toBe('held by ops')
    const unentitled = authorCluster({ onOpen: () => {}, authored, writeEntitled: false })
    expect(unentitled.tools[1].disabled).toBe(true)
    expect(unentitled.tools[1].reason).toBe(REASONS.writeUnentitled)
  })

  it('leaves an authored read tool live under a write lock and without write entitlement', () => {
    const readTool = { name: 'panel-audit', catalog_digest: 'sha256:abc', capabilities: ['drawing.read'] }
    const cluster = authorCluster({ onOpen: () => {}, authored: readTool, writeLocked: true, writeEntitled: false })
    expect(cluster.tools[1].disabled).toBe(false)
    expect(cluster.tools[1].reason).toBe('')
  })
})

// W4g-7b-05c: the flag-off reference panels' census. Dimensions is gone
// (real since 04c); the four deferred controls carry their own reasons, not
// the generic REASONS.notInEngine every other placeholder here still does.
describe('referencePanels (the flag-off placeholders)', () => {
  it('Annotation drops the real Dimensions and Leader placeholders; Text stays generic', () => {
    const [annotation] = referencePanels()
    expect(toolsOf(annotation)).not.toHaveProperty('annotation:dimensions')
    expect(toolsOf(annotation)['annotation:text'].reason).toBe(REASONS.notInEngine)
    expect(toolsOf(annotation)).not.toHaveProperty('annotation:leader')
  })

  it('Block uses the engine-off reason for both real commands', () => {
    const [, block] = referencePanels()
    const create = toolsOf(block)['draw:createBlock']
    expect(create.disabled).toBe(true)
    expect(create.reason).toBe(REASONS.notInEngine)
    expect(toolsOf(block)['block:insert'].reason).toBe(REASONS.notInEngine)
  })

  it('Groups gives Group and Ungroup their own reason', () => {
    const [, , , groups] = referencePanels()
    const group = toolsOf(groups)['groups:group']
    const ungroup = toolsOf(groups)['groups:ungroup']
    expect([group.disabled, group.reason]).toEqual([true, REASONS.notInEngine])
    expect([ungroup.disabled, ungroup.reason]).toEqual([true, REASONS.notInEngine])
  })
})
