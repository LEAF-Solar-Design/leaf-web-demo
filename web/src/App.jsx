import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import Viewer from './components/Viewer.jsx'
import Legend from './components/Legend.jsx'
import ToolsPanel from './components/ToolsPanel.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import AuthorPanel from './components/AuthorPanel.jsx'
import SelectionReadout from './components/SelectionReadout.jsx'
import PromptBox from './components/PromptBox.jsx'
import RoutePanel from './components/RoutePanel.jsx'
import JobRail from './components/JobRail.jsx'
import DegradedBanner from './components/DegradedBanner.jsx'
import EntitlementGate from './components/EntitlementGate.jsx'
import {
  config, getSession, getTools, runTool, runToolAsync, attachToJob, getJob, listJobs,
  recordToEnvelope, authorTool, getDrawingIntake, undoDrawing, redoDrawing, nlPrompt,
} from './api.js'
import { matchPrompt } from './mock/mockNlPrompt.js'
import { editFixture, pendingEditDemo, editFixtureV2 } from './mock/editFixture.js'

// Calm, muted ink palette — reads on the light "paper" CADViewport canvas.
const PALETTE = ['#3b6ea5', '#5f8a6a', '#8a6ea0', '#b08a4a', '#a5637a', '#4f8a94']

// Durable pointer to the one in-flight live job, so a closed/reloaded tab can
// re-attach instead of orphaning the UI (CONTRACT-ADDENDUM §7, MATRIX gap #1).
const INFLIGHT_KEY = 'leaf.inflightJob'
const saveInflight = (job_id, tool) => {
  try { localStorage.setItem(INFLIGHT_KEY, JSON.stringify({ job_id, tool, ts: Date.now() })) } catch { /* noop */ }
}
const clearInflight = () => {
  try { localStorage.removeItem(INFLIGHT_KEY) } catch { /* noop */ }
}
const readInflight = () => {
  try { return JSON.parse(localStorage.getItem(INFLIGHT_KEY) || 'null') } catch { return null }
}

// `?fixture=edit` (mock only) loads the synthetic edit fixture that exercises
// inserts + 3DFACEs + picking + the pending/version flow.
const fixtureParam = new URLSearchParams(window.location.search).get('fixture')

// `?demo=degraded` is a DEV-only demo hook to show the §10 degraded banner
// without a real APS_LIVE=1 fallback (degraded_mode is only true when a cloud
// run falls back; at APS_LIVE=0 it never trips). It only forces the banner's
// visibility — it fabricates no result numbers.
const demoDegraded = new URLSearchParams(window.location.search).get('demo') === 'degraded'

// Collapsible left-rail section (keeps the classic catalog reachable but
// secondary to the prompt box — the primary path).
function Section({ title, count, open, onToggle, children, innerRef }) {
  return (
    <div className={`section ${open ? '' : 'collapsed'}`} ref={innerRef}>
      <button className="section-head" onClick={onToggle} aria-expanded={open}>
        <span>{title}{count != null ? <span className="n"> · {count}</span> : null}</span>
        <span className="chev">{open ? 'hide' : 'show'}</span>
      </button>
      {open && <div className="section-body">{children}</div>}
    </div>
  )
}

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
  const [runStatus, setRunStatus] = useState(null)   // live job phase: 'submitted' | 'running'
  const [runElapsedMs, setRunElapsedMs] = useState(null) // ticking wall-clock while running
  const [result, setResult] = useState(null)
  const [runErr, setRunErr] = useState(null)
  const [selectedHandle, setSelectedHandle] = useState(null)
  const [pendingEdit, setPendingEdit] = useState(null)
  // Write-loop (§11, live mode): the current drawing/version chain from the last
  // drawing response, a calm inline note, and an undo/redo-in-flight guard.
  const [drawingState, setDrawingState] = useState(null) // {drawing_id, version, head, latest}
  const [versionNote, setVersionNote] = useState(null)   // e.g. "version 2 created"
  const [versionBusy, setVersionBusy] = useState(false)  // undo/redo request in flight
  const [overlayStale, setOverlayStale] = useState(false) // last result overlay no longer matches shown version
  const [openTool, setOpenTool] = useState(null)         // the tool card expanded in ToolsPanel (for the write ghost)

  // --- unified surface state ---
  const [tenant, setTenant] = useState(null)             // /api/session tenant echo (else "demo")
  const [prompt, setPrompt] = useState('')               // dispatch box text
  const [route, setRoute] = useState(null)               // §12 nl-prompt routing decision
  const [routing, setRouting] = useState(false)          // awaiting the router
  const [jobs, setJobs] = useState([])                   // live rail: recent GET /api/jobs
  const [currentJobId, setCurrentJobId] = useState(null) // this session's live job id (dedupe)
  const [inflightPtr, setInflightPtr] = useState(null)   // localStorage re-attach pointer
  const [reattaching, setReattaching] = useState(false)  // auto re-attach in progress
  const [toolsOpen, setToolsOpen] = useState(false)      // left catalog collapsed by default
  const [authorOpen, setAuthorOpen] = useState(false)    // author flow (opens on build lane)
  const [authorSeed, setAuthorSeed] = useState('')       // build-lane prefill text
  const [authorSignal, setAuthorSignal] = useState(0)    // bump to re-seed the author flow

  const viewerRef = useRef(null)
  const authorSectionRef = useRef(null)
  const runningSinceRef = useRef(null)  // ms epoch the job entered 'running'
  const lastRunRef = useRef(null)       // {tool, params} for the retry affordance

  const isEditFixture = mock && fixtureParam === 'edit'
  // What the panels/legend/selection reflect: the applied version if present.
  const shown = versionIntake || intake
  const projectName = shown?.dwg ? shown.dwg.split(/[\\/]/).pop().replace(/\.dwg$/i, '') : 'your project'
  const tierLabel = tenant || 'demo'

  // color-by-layer, stable across renders (keyed to base intake identity)
  const colorForLayer = useMemo(() => {
    const layers = intake?.layers || []
    const map = {}
    layers.forEach((l, i) => { map[l] = PALETTE[i % PALETTE.length] })
    return (layer) => map[layer] || '#8aa0b5'
  }, [intake])

  // load session (intake + tenant echo) + reset transient state on mode/fixture change
  useEffect(() => {
    let alive = true
    setIntake(null); setVersionIntake(null); setLoadErr(null)
    setResult(null); setSelectedHandle(null); setPendingEdit(null)
    setRunning(false); setRunStatus(null); setRunElapsedMs(null); setRunErr(null)
    setDrawingState(null); setVersionNote(null); setVersionBusy(false)
    setOverlayStale(false); setOpenTool(null)
    setRoute(null); setRouting(false); setCurrentJobId(null); setTenant(null)
    runningSinceRef.current = null
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
      .then(({ intake: d, tenant: t }) => { if (!alive) return; seat(d); setTenant(t) })
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

  // Refresh the live job rail on demand (after a run completes, so the job shows
  // immediately rather than waiting for the next poll). No-op in mock.
  const refreshJobs = useCallback(async () => {
    if (mock) return
    try { setJobs(await listJobs()) } catch { /* transient */ }
  }, [mock])

  // Poll the recent-jobs list (live only; zero /api calls in mock).
  useEffect(() => {
    if (mock) { setJobs([]); return }
    let alive = true
    const tick = async () => {
      try { const js = await listJobs(); if (alive) setJobs(js) } catch { /* transient */ }
    }
    tick()
    const id = setInterval(tick, 2500)
    return () => { alive = false; clearInterval(id) }
  }, [mock])

  // A live job entered 'running' — begin (or resume) the wall-clock, using the
  // server's started_at when known (localhost clocks match) so a re-attach
  // continues the real elapsed time instead of restarting at zero.
  const markRunning = useCallback((startedAtSec) => {
    if (runningSinceRef.current == null) {
      runningSinceRef.current = startedAtSec ? startedAtSec * 1000 : Date.now()
    }
    setRunStatus('running')
    setRunElapsedMs(Math.max(0, Date.now() - runningSinceRef.current))
  }, [])

  // Tick the calm "running · N.Ns" clock while a live job runs (no animated loader).
  useEffect(() => {
    if (runStatus !== 'running') return
    const id = setInterval(() => {
      if (runningSinceRef.current != null) setRunElapsedMs(Date.now() - runningSinceRef.current)
    }, 200)
    return () => clearInterval(id)
  }, [runStatus])

  // Tab-close survivability: on load in live mode, if a durable in-flight job
  // pointer exists, re-attach. Terminal already -> render its envelope; still
  // running -> resume calm progress + final render. The rail shows a re-attach
  // chip while this runs. Clear the pointer either way.
  useEffect(() => {
    if (mock) { setInflightPtr(null); setReattaching(false); return }
    const saved = readInflight()
    if (!saved || !saved.job_id) return
    setInflightPtr(saved)
    let alive = true
    ;(async () => {
      let rec
      try {
        rec = await getJob(saved.job_id)
      } catch {
        clearInflight(); setInflightPtr(null) // 404 / unreachable -> stale pointer
        return
      }
      if (!alive) return
      setSelectedTool((t) => t || { name: saved.tool })
      setCurrentJobId(saved.job_id)
      if (rec.status === 'complete' || rec.status === 'failed') {
        setResult(recordToEnvelope(rec))
        clearInflight(); setInflightPtr(null)
        return
      }
      // still in flight — resume progress and await the terminal envelope
      setRunning(true); setRunErr(null); setResult(null); setReattaching(true)
      if (rec.status === 'running') markRunning(rec.started_at)
      else setRunStatus(rec.status || 'submitted')
      try {
        const env = await attachToJob(saved.job_id, {
          onStatus: (st) => {
            if (!alive) return
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
        if (alive) setResult(env)
      } catch (e) {
        if (alive) setRunErr(String(e.message || e))
      } finally {
        if (alive) {
          setRunning(false); setRunStatus(null); setRunElapsedMs(null); runningSinceRef.current = null
          setReattaching(false); setInflightPtr(null)
        }
        clearInflight()
        refreshJobs()
      }
    })()
    return () => { alive = false }
  }, [mock, markRunning, refreshJobs])

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

  // Swap the viewer + panels to a drawing version (§11).
  const seatVersion = useCallback((view, drawingId, note) => {
    viewerRef.current?.applyVersion(view.intake)
    setVersionIntake(view.intake)
    setPendingEdit(null)
    setSelectedHandle(null)
    setDrawingState({ drawing_id: drawingId, version: view.head, head: view.head, latest: view.latest })
    setVersionNote(note)
  }, [])

  const onUndo = useCallback(async () => {
    if (!drawingState || versionBusy) return
    setVersionBusy(true); setOverlayStale(true)
    try {
      const view = await undoDrawing(drawingState.drawing_id)
      seatVersion(view, drawingState.drawing_id, `reverted to version ${view.head}`)
    } catch (e) {
      setRunErr(String(e.message || e))
    } finally {
      setVersionBusy(false)
    }
  }, [drawingState, versionBusy, seatVersion])

  const onRedo = useCallback(async () => {
    if (!drawingState || versionBusy) return
    setVersionBusy(true); setOverlayStale(true)
    try {
      const view = await redoDrawing(drawingState.drawing_id)
      seatVersion(view, drawingState.drawing_id, `advanced to version ${view.head}`)
    } catch (e) {
      setRunErr(String(e.message || e))
    } finally {
      setVersionBusy(false)
    }
  }, [drawingState, versionBusy, seatVersion])

  const onRun = useCallback(async (tool, params) => {
    setSelectedTool(tool)
    setRunning(true); setRunErr(null); setResult(null)
    setRunStatus(null); setRunElapsedMs(null); runningSinceRef.current = null
    setOverlayStale(false); setCurrentJobId(null)
    lastRunRef.current = { tool, params }
    // feed the picked entity to the tool so an edit tool can target it. A write
    // tool (delete-marked-panel) reads `handle`, so map the selection onto it
    // too — that makes the pending ghost's previewed deletion the real target.
    const isWrite = (tool.capabilities || []).includes('drawing.write')
    const merged = selectedHandle
      ? { ...(params || {}), target_handle: selectedHandle, ...(isWrite ? { handle: selectedHandle } : {}) }
      : params
    try {
      let env
      if (mock) {
        // Mock stays fully client-side — no jobs API, no progress phases.
        env = await runTool(mock, tool, merged, shown)
      } else {
        env = await runToolAsync(tool, merged, 'rooftop_demo', {
          onSubmit: (job_id) => { saveInflight(job_id, tool.name); setCurrentJobId(job_id) },
          onStatus: (st) => {
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
      }
      setResult(env)
      // Write loop (§11): a drawing.write run stamps result.new_version. Fetch
      // the fresh head intake and swap the viewer to the new version.
      if (!mock && env?.ok && env.result?.new_version) {
        const nv = env.result.new_version
        try {
          const view = await getDrawingIntake(nv.drawing_id, 'head')
          seatVersion(view, nv.drawing_id, `version ${nv.version} created`)
        } catch {
          setVersionNote(`version ${nv.version} created (viewer refresh failed)`)
        }
      }
    } catch (e) {
      setRunErr(String(e.message || e))
    } finally {
      setRunning(false)
      setRunStatus(null); setRunElapsedMs(null); runningSinceRef.current = null
      if (!mock) { clearInflight(); refreshJobs() }
    }
  }, [mock, shown, selectedHandle, markRunning, seatVersion, refreshJobs])

  // Retry the last run (plain affordance for retryable failures / transport hiccups).
  const onRetry = useCallback(() => {
    const last = lastRunRef.current
    if (last) onRun(last.tool, last.params)
  }, [onRun])

  const onAuthor = useCallback(async (description) => {
    const res = await authorTool(mock, description)
    setTools((prev) => {
      const rest = prev.filter((t) => t.name !== res.tool.name)
      return [...rest, res.tool]
    })
    return res
  }, [mock])

  // --- prompt-first dispatch (§12) -----------------------------------------
  // Live preview of which lane the text will route to (lights the hero's dots).
  const hintLane = useMemo(() => {
    const s = prompt.trim()
    if (!s) return null
    try { return matchPrompt(s, tools).lane } catch { return null }
  }, [prompt, tools])

  const onDispatch = useCallback(async () => {
    const text = prompt.trim()
    if (!text || routing) return
    setRouting(true); setRoute(null)
    try {
      const r = await nlPrompt(mock, text, tools)
      setRoute(r)
      if (r.lane === 'build') {
        setAuthorSeed(text)
        setAuthorSignal((n) => n + 1)
        setAuthorOpen(true)
        // bring the (left-rail) author flow into view
        setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
      }
    } catch (e) {
      setRoute({
        lane: 'run', tool: null, params: {}, confidence: 0, alternatives: [],
        rationale: `Routing failed: ${e.message || e}`,
      })
    } finally {
      setRouting(false)
    }
  }, [prompt, routing, mock, tools])

  // Pick an alternative from a low-confidence / live-only route -> a user-picked
  // (high-confidence) run route for that capability.
  const onPickAlternative = useCallback((name) => {
    setRoute((prev) => ({
      lane: 'run', tool: name, params: {}, confidence: 0.99,
      rationale: 'You picked this capability from the alternatives.',
      alternatives: (prev?.alternatives || []).filter((a) => a.tool !== name),
      stub: prev?.stub, stubReason: prev?.stubReason,
    }))
  }, [])

  const onOpenAuthor = useCallback(() => {
    setAuthorOpen(true)
    setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
  }, [])

  // Click a terminal job in the rail -> re-render its stored envelope (§3/§7).
  const onSelectJob = useCallback(async (job) => {
    if (!job || (job.status !== 'complete' && job.status !== 'failed')) return
    try {
      const rec = await getJob(job.job_id)
      setSelectedTool({ name: rec.tool || job.tool })
      setResult(recordToEnvelope(rec))
      setRunErr(null); setRunning(false); setOverlayStale(false)
      setCurrentJobId(job.job_id)
    } catch (e) {
      setRunErr(String(e.message || e))
    }
  }, [])

  const toggleLayer = useCallback((layer) => {
    setVisibleLayers((v) => ({ ...v, [layer]: !v[layer] }))
  }, [])

  const applyVersion = useCallback(() => {
    viewerRef.current?.applyVersion(editFixtureV2)
    setVersionIntake(editFixtureV2)
    setPendingEdit(null)
    setSelectedHandle(null)
  }, [])

  // The last run's overlay only describes the version it produced; once the user
  // undoes/redoes to a different version, suppress it so the viewer never shows a
  // stale "deleted" marker over restored geometry (the result receipt still shows).
  const overlay = (result && !overlayStale) ? (result.overlay || null) : null
  const applied = versionIntake != null

  // Pending-edit ghost (live, §11 nicety): when an open write tool + a picked
  // handle line up, preview the deletion before Run.
  const writeGhost = useMemo(() => {
    if (mock || !selectedHandle) return null
    const caps = openTool?.capabilities || []
    return caps.includes('drawing.write') ? { removed: [selectedHandle] } : null
  }, [mock, selectedHandle, openTool])

  // The current in-session run, shown as a rail card until it appears in the
  // polled live list (deduped by job_id in JobRail).
  const currentJob = useMemo(() => {
    const toolName = selectedTool?.name
    if (!toolName) return null
    let status
    if (running) status = runStatus || 'running'
    else if (result) status = result.ok ? 'complete' : 'failed'
    else return null
    return {
      job_id: currentJobId,
      tool: toolName,
      status,
      progress: runStatus || 'running',
      elapsed_ms: runElapsedMs != null ? runElapsedMs : (result?.timing_ms ?? null),
      degraded_mode: !!result?.degraded_mode,
      error: result?.error || null,
    }
  }, [selectedTool, running, runStatus, result, runElapsedMs, currentJobId])

  const degraded = !!result?.degraded_mode || demoDegraded

  return (
    <div className="app">
      <header className="top">
        <div className="mark"><span className="diamond" aria-hidden="true" /> LEAF / unified surface</div>
        <div className="proj">
          <span className="tag">Project</span>
          <span className="name">{projectName}</span>
          <span className="meta">
            {shown ? `${shown.polylines.length} polylines · ${shown.layers.length} layers` : 'loading'}
          </span>
          <span className={`tag ${mock ? '' : 'live'}`}>{mock ? 'mock data' : 'in design'}</span>
        </div>
        <div className="spacer" />
        <div className="who">
          <span className={`mode-label ${mock ? '' : 'live'}`}>
            {mock ? 'mock' : `live · ${config.apiBase}`}
          </span>
          <label className="switch" title="Toggle mock vs live backend">
            <input type="checkbox" checked={mock} onChange={(e) => setMock(e.target.checked)} />
            <span>Mock</span>
          </label>
          <span>tier <b>{tierLabel}</b></span>
        </div>
      </header>

      <aside className="nav">
        <div className="fam-title">Catalog · {tools.length} caps</div>
        <Section title="Tools" count={tools.length} open={toolsOpen} onToggle={() => setToolsOpen((o) => !o)}>
          <ToolsPanel
            tools={tools}
            error={toolsErr}
            running={running}
            selectedTool={selectedTool}
            onRun={onRun}
            onOpenTool={setOpenTool}
          />
        </Section>
        <Section
          title="Author a tool"
          open={authorOpen}
          onToggle={() => setAuthorOpen((o) => !o)}
          innerRef={authorSectionRef}
        >
          <AuthorPanel
            onAuthor={onAuthor}
            onRunAuthored={onRun}
            seed={authorSeed}
            seedSignal={authorSignal}
          />
        </Section>
      </aside>

      <main>
        <div className="kicker">Home · one prompt, three lanes</div>
        <h1 className="home-q">What should Leaf do to <em>{projectName}</em>?</h1>

        <PromptBox
          value={prompt}
          onChange={setPrompt}
          onDispatch={onDispatch}
          routing={routing}
          hintLane={hintLane}
        />
        <div className="hint">
          One prompt, routed across <b>Run</b> · <b>Solve</b> · <b>Build</b>. You confirm before
          anything runs — paid actions never auto-execute.
        </div>

        <RoutePanel
          route={route}
          tools={tools}
          running={running}
          onRun={onRun}
          onPickAlternative={onPickAlternative}
          onOpenAuthor={onOpenAuthor}
        />

        {degraded && <DegradedBanner />}

        <div className="workspace-card">
          <div className="viewer-toolbar">
            <div className="viewer-title">
              {shown ? `${projectName}.dwg` : 'loading…'}
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
              {!mock && versionNote && <span className="version-note">{versionNote}</span>}
              {!mock && drawingState && (
                <>
                  <button
                    className="btn ghost"
                    onClick={onUndo}
                    disabled={versionBusy || running || drawingState.head <= 1}
                  >
                    Undo
                  </button>
                  <button
                    className="btn ghost"
                    onClick={onRedo}
                    disabled={versionBusy || running || drawingState.head >= drawingState.latest}
                  >
                    Redo
                  </button>
                </>
              )}
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
                pendingEdit={pendingEdit || writeGhost}
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
        </div>

        <div className="result-block">
          <ResultPanel
            running={running}
            runStatus={runStatus}
            runElapsedMs={runElapsedMs}
            error={runErr}
            result={result}
            tool={selectedTool}
            onRetry={onRetry}
          />
        </div>

        <EntitlementGate tier={tierLabel} />
      </main>

      <JobRail
        mock={mock}
        jobs={jobs}
        currentJob={currentJob}
        inflight={inflightPtr}
        reattaching={reattaching}
        onSelectJob={onSelectJob}
      />

      <footer className="foot-bar">
        <span>capability catalog · <span className="ok-txt">{tools.length} loaded</span></span>
        <span>backend · {mock ? <span className="warn-txt">mock (no cloud)</span> : <span className="ok-txt">live</span>}</span>
        <span>local solver · <span className="ok-txt">ready</span></span>
        <span>entitlement · tier {tierLabel}</span>
        <span style={{ marginLeft: 'auto' }}>build v2026.07 · {mock ? 'sample data' : 'live'}</span>
      </footer>
    </div>
  )
}
