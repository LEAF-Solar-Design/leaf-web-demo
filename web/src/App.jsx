import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import Viewer from './components/Viewer.jsx'
import Legend from './components/Legend.jsx'
import ToolsPanel from './components/ToolsPanel.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import AuthorPanel from './components/AuthorPanel.jsx'
import SelectionReadout from './components/SelectionReadout.jsx'
import { config, getSession, getTools, runTool, authorTool } from './api.js'
import { editFixture, pendingEditDemo, editFixtureV2 } from './mock/editFixture.js'

// Calm, muted ink palette — reads on the light "paper" CADViewport canvas.
const PALETTE = ['#3b6ea5', '#5f8a6a', '#8a6ea0', '#b08a4a', '#a5637a', '#4f8a94']

// `?fixture=edit` (mock only) loads the synthetic edit fixture that exercises
// inserts + 3DFACEs + picking + the pending/version flow.
const fixtureParam = new URLSearchParams(window.location.search).get('fixture')

export default function App() {
  const [mock, setMock] = useState(config.mockDefault)
  const [intake, setIntake] = useState(null)
  const [versionIntake, setVersionIntake] = useState(null) // applied next-version
  const [loadErr, setLoadErr] = useState(null)
  const [tools, setTools] = useState([])
  const [toolsErr, setToolsErr] = useState(null)
  const [visibleLayers, setVisibleLayers] = useState({})
  const [selectedTool, setSelectedTool] = useState(null)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [runErr, setRunErr] = useState(null)
  const [selectedHandle, setSelectedHandle] = useState(null)
  const [pendingEdit, setPendingEdit] = useState(null)
  const viewerRef = useRef(null)

  const isEditFixture = mock && fixtureParam === 'edit'
  // What the panels/legend/selection reflect: the applied version if present.
  const shown = versionIntake || intake

  // color-by-layer, stable across renders (keyed to base intake identity)
  const colorForLayer = useMemo(() => {
    const layers = intake?.layers || []
    const map = {}
    layers.forEach((l, i) => { map[l] = PALETTE[i % PALETTE.length] })
    return (layer) => map[layer] || '#8aa0b5'
  }, [intake])

  // load session (intake) + reset transient state when mode/fixture changes
  useEffect(() => {
    let alive = true
    setIntake(null); setVersionIntake(null); setLoadErr(null)
    setResult(null); setSelectedHandle(null); setPendingEdit(null)
    const seat = (d) => {
      if (!alive) return
      setIntake(d)
      const vis = {}
      for (const l of d.layers || []) vis[l] = true
      setVisibleLayers(vis)
    }
    if (isEditFixture) {
      seat(editFixture) // synchronous local fixture — no backend
      return () => { alive = false }
    }
    getSession(mock)
      .then(seat)
      .catch((e) => alive && setLoadErr(String(e.message || e)))
    return () => { alive = false }
  }, [mock, isEditFixture])

  useEffect(() => {
    let alive = true
    setToolsErr(null)
    getTools(mock)
      .then((t) => alive && setTools(t))
      .catch((e) => alive && setToolsErr(String(e.message || e)))
    return () => { alive = false }
  }, [mock])

  const layerCounts = useMemo(() => {
    const c = {}
    for (const l of shown?.layers || []) c[l] = 0
    for (const pl of shown?.polylines || []) c[pl.layer] = (c[pl.layer] || 0) + 1
    return c
  }, [shown])

  // resolve the picked handle to an entity descriptor for the readout
  const selection = useMemo(() => {
    if (!selectedHandle || !shown) return null
    const pl = shown.polylines?.find((p) => p.handle === selectedHandle)
    if (pl) return { handle: pl.handle, kind: 'polyline', layer: pl.layer }
    const ins = shown.inserts?.find((i) => i.handle === selectedHandle)
    if (ins) return { handle: ins.handle, kind: 'insert', layer: ins.layer, name: ins.name }
    const f = shown.faces3d?.find((x) => x.handle === selectedHandle)
    if (f) return { handle: f.handle, kind: '3dface', layer: f.layer }
    return { handle: selectedHandle, kind: 'entity', layer: null }
  }, [selectedHandle, shown])

  const onRun = useCallback(async (tool, params) => {
    setSelectedTool(tool)
    setRunning(true); setRunErr(null); setResult(null)
    // feed the picked entity to the tool so an edit tool can target it
    const merged = selectedHandle ? { ...(params || {}), target_handle: selectedHandle } : params
    try {
      const env = await runTool(mock, tool, merged, shown)
      if (env && env.ok === false) setRunErr(env.error || 'tool run failed')
      setResult(env)
    } catch (e) {
      setRunErr(String(e.message || e))
    } finally {
      setRunning(false)
    }
  }, [mock, shown, selectedHandle])

  const onAuthor = useCallback(async (description) => {
    const res = await authorTool(mock, description)
    setTools((prev) => {
      const rest = prev.filter((t) => t.name !== res.tool.name)
      return [...rest, res.tool]
    })
    return res
  }, [mock])

  const toggleLayer = useCallback((layer) => {
    setVisibleLayers((v) => ({ ...v, [layer]: !v[layer] }))
  }, [])

  const applyVersion = useCallback(() => {
    // exercise the viewer's imperative version-apply path, then clear the
    // optimistic ghost and stale selection; panels reflect the new version.
    viewerRef.current?.applyVersion(editFixtureV2)
    setVersionIntake(editFixtureV2)
    setPendingEdit(null)
    setSelectedHandle(null)
  }, [])

  const overlay = result?.overlay || null
  const applied = versionIntake != null

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>Leaf</h1>
            <p>build CAD tools with AI, run on Leaf</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`mode-label ${mock ? 'mock' : 'live'}`}>
            {mock ? 'mock data' : `live · ${config.apiBase}`}
          </span>
          <label className="switch" title="Toggle mock vs live backend">
            <input type="checkbox" checked={mock} onChange={(e) => setMock(e.target.checked)} />
            <span>Mock mode</span>
          </label>
        </div>
      </header>

      <div className="workspace">
        <aside className="left">
          <ToolsPanel
            tools={tools}
            error={toolsErr}
            running={running}
            selectedTool={selectedTool}
            onRun={onRun}
          />
          <AuthorPanel onAuthor={onAuthor} onRunAuthored={onRun} />
        </aside>

        <main className="center">
          <div className="viewer-toolbar">
            <div className="viewer-title">
              {shown ? shown.dwg?.split(/[\\/]/).pop() : 'loading…'}
              {shown && (
                <span className="dim">
                  {' · '}{shown.polylines.length} polylines
                  {shown.inserts?.length ? ` · ${shown.inserts.length} inserts` : ''}
                  {shown.faces3d?.length ? ` · ${shown.faces3d.length} faces` : ''}
                  {' · '}{shown.layers.length} layers
                </span>
              )}
            </div>
            <div className="viewer-actions">
              {isEditFixture && (
                <>
                  <button
                    className="btn ghost"
                    onClick={() => setPendingEdit((p) => (p ? null : pendingEditDemo))}
                    disabled={applied}
                  >
                    {pendingEdit ? 'Hide pending edit' : 'Preview pending edit'}
                  </button>
                  <button className="btn ghost" onClick={applyVersion} disabled={applied}>
                    {applied ? 'Version applied' : 'Apply version'}
                  </button>
                </>
              )}
              <button className="btn ghost" onClick={() => viewerRef.current?.fit()}>Fit to bounds</button>
            </div>
          </div>
          <div className="viewer-wrap">
            {loadErr && <div className="overlay-msg error">Failed to load drawing: {loadErr}</div>}
            {!intake && !loadErr && <div className="overlay-msg">Loading drawing…</div>}
            {intake && (
              <Viewer
                ref={viewerRef}
                intake={intake}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                highlightHandles={overlay?.highlight_handles}
                markers={overlay?.markers}
                overlayPolylines={overlay?.polylines}
                selectedHandle={selectedHandle}
                onSelectEntity={setSelectedHandle}
                pendingEdit={pendingEdit}
              />
            )}
            {shown && (
              <Legend
                layers={shown.layers}
                counts={layerCounts}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                onToggle={toggleLayer}
              />
            )}
            {intake && (
              <SelectionReadout selection={selection} onDeselect={() => setSelectedHandle(null)} />
            )}
          </div>
        </main>

        <aside className="right">
          <ResultPanel
            running={running}
            error={runErr}
            result={result}
            tool={selectedTool}
          />
        </aside>
      </div>
    </div>
  )
}
