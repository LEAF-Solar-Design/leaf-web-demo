import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import Viewer from './components/Viewer.jsx'
import Legend from './components/Legend.jsx'
import ToolsPanel from './components/ToolsPanel.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import AuthorPanel from './components/AuthorPanel.jsx'
import { config, getSession, getTools, runTool, authorTool } from './api.js'

const PALETTE = ['#4da3ff', '#5be3a7', '#c792ea', '#ffca6b', '#ff7b9c', '#6bd5ff']

export default function App() {
  const [mock, setMock] = useState(config.mockDefault)
  const [intake, setIntake] = useState(null)
  const [loadErr, setLoadErr] = useState(null)
  const [tools, setTools] = useState([])
  const [toolsErr, setToolsErr] = useState(null)
  const [visibleLayers, setVisibleLayers] = useState({})
  const [selectedTool, setSelectedTool] = useState(null)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [runErr, setRunErr] = useState(null)
  const viewerRef = useRef(null)

  // color-by-layer, stable across renders (keyed to intake identity)
  const colorForLayer = useMemo(() => {
    const layers = intake?.layers || []
    const map = {}
    layers.forEach((l, i) => { map[l] = PALETTE[i % PALETTE.length] })
    return (layer) => map[layer] || '#8aa0b5'
  }, [intake])

  // load session (intake) + tools whenever mock mode changes
  useEffect(() => {
    let alive = true
    setIntake(null); setLoadErr(null); setResult(null)
    getSession(mock)
      .then((d) => {
        if (!alive) return
        setIntake(d)
        const vis = {}
        for (const l of d.layers || []) vis[l] = true
        setVisibleLayers(vis)
      })
      .catch((e) => alive && setLoadErr(String(e.message || e)))
    return () => { alive = false }
  }, [mock])

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
    for (const l of intake?.layers || []) c[l] = 0
    for (const pl of intake?.polylines || []) c[pl.layer] = (c[pl.layer] || 0) + 1
    return c
  }, [intake])

  const onRun = useCallback(async (tool, params) => {
    setSelectedTool(tool)
    setRunning(true); setRunErr(null); setResult(null)
    try {
      const env = await runTool(mock, tool, params, intake)
      if (env && env.ok === false) setRunErr(env.error || 'tool run failed')
      setResult(env)
    } catch (e) {
      setRunErr(String(e.message || e))
    } finally {
      setRunning(false)
    }
  }, [mock, intake])

  const onAuthor = useCallback(async (description) => {
    const res = await authorTool(mock, description)
    // add to tool list (dedupe by name — newest wins)
    setTools((prev) => {
      const rest = prev.filter((t) => t.name !== res.tool.name)
      return [...rest, res.tool]
    })
    return res
  }, [mock])

  const toggleLayer = useCallback((layer) => {
    setVisibleLayers((v) => ({ ...v, [layer]: !v[layer] }))
  }, [])

  const overlay = result?.overlay || null

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="leaf">🍃</span>
          <div>
            <h1>Leaf</h1>
            <p>build CAD tools with AI, run on Leaf</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`mode-pill ${mock ? 'mock' : 'live'}`}>
            {mock ? 'MOCK DATA' : `LIVE · ${config.apiBase}`}
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
              {intake ? intake.dwg?.split(/[\\/]/).pop() : 'loading…'}
              {intake && <span className="dim"> · {intake.polylines.length} polylines · {intake.layers.length} layers</span>}
            </div>
            <button className="btn ghost" onClick={() => viewerRef.current?.fit()}>Fit to bounds</button>
          </div>
          <div className="viewer-wrap">
            {loadErr && <div className="overlay-msg error">Failed to load drawing: {loadErr}</div>}
            {!intake && !loadErr && <div className="overlay-msg">Loading rooftop_demo…</div>}
            {intake && (
              <Viewer
                ref={viewerRef}
                intake={intake}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                highlightHandles={overlay?.highlight_handles}
                markers={overlay?.markers}
                overlayPolylines={overlay?.polylines}
              />
            )}
            {intake && (
              <Legend
                layers={intake.layers}
                counts={layerCounts}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                onToggle={toggleLayer}
              />
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
