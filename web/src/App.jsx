import './structural.css'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, Suspense } from 'react'
// The 3D viewer drags in `three`; loading it lazily (mirroring the auth.js
// dynamic-import pattern) keeps first paint off the critical path.
const Viewer = React.lazy(() => import('./components/Viewer.jsx'))
import Legend from './components/Legend.jsx'
import ToolsPanel from './components/ToolsPanel.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import AuthorPanel from './components/AuthorPanel.jsx'
import SelectionReadout from './components/SelectionReadout.jsx'
import PromptBox from './components/PromptBox.jsx'
import RoutePanel from './components/RoutePanel.jsx'
import JobRail from './components/JobRail.jsx'
import DegradedBanner from './components/DegradedBanner.jsx'
import EntitlementGate, { EntitlementNotice } from './components/EntitlementGate.jsx'
import QuotaCard from './components/QuotaCard.jsx'
import VersionHistory from './components/VersionHistory.jsx'
import * as mockVersions from './mock/mockVersions.js'
import ProjectSwitcher from './components/ProjectSwitcher.jsx'
import WorkspaceSummary from './components/WorkspaceSummary.jsx'
import OpsDrawer from './components/OpsDrawer.jsx'
import CheckoutControls from './components/CheckoutControls.jsx'
import ClaudeAccountPanel from './components/ClaudeAccountPanel.jsx'
import DemoBanner from './components/DemoBanner.jsx'
import { authConfigured, login, logout, isSignedIn, handleRedirectCallback } from './auth.js'
import { shouldAutoDemo } from './demoState.js'
import { humanizeError } from './errorHumanize.js'
import {
  confirmRunIntent, createCatalogRunContext, createCatalogToolSnapshot, createRunIntentState,
  dismissRunIntent, stageRunIntent,
} from './runIntent.js'
import useExit from './useExit.js'
import Toast from './components/Toast.jsx'
import DetailsDrawer from './components/DetailsDrawer.jsx'
import {
  config, getSession, getTools, getCapabilities, getUsage, getHealth, runTool, runToolAsync,
  attachToJob, getJob, listJobs, recordToEnvelope, stageAuthorTool, publishStagedAuthor, getDrawingIntake,
  getDrawingVersions, undoDrawing, redoDrawing, takeCheckout, releaseCheckout, nlPrompt, closeJobBeacon,
  getStoredOrgId, setStoredOrgId, createOrg, listProjects, createProject, openProject,
  getClaudeGrant, linkClaudeGrant, unlinkClaudeGrant, getEntitlements,
} from './api.js'
import { matchPrompt } from './mock/mockNlPrompt.js'
import { shouldStartTour } from './demo/tourEntry.js'
import DemoTour from './demo/DemoTour.jsx'
import { editFixture, pendingEditDemo, editFixtureV2 } from './mock/editFixture.js'
import ConversePanel from './components/ConversePanel.jsx'
import { THRESHOLDS, classifyAgentError } from './converse.js'
import useConverseSessionController from './controllers/useConverseSessionController.js'

// Calm layer palette, re-derived at higher lightness for the DARK CADViewport
// canvas (--cv-bg #0f0f11) — same hue spacing as the retired light-paper set so
// the legend swatches stay distinguishable.
const PALETTE = ['#6b9fd4', '#8fbf9c', '#b49bd1', '#d4af6e', '#cf8fa6', '#79bcc7']

// Suspense fallback while the lazy viewer chunk arrives — L1 indeterminate:
// pulse dot + verb, top-left (the centered position is reserved for X3 failures).
function ViewerSkeleton() {
  return (
    <div aria-hidden="true" style={{ position: 'absolute', inset: 0, background: '#0f0f11' }}>
      <div className="loading-line dim" style={{ position: 'absolute', top: 14, left: 14 }}>
        <span className="dot live pulse" aria-hidden="true" /> Preparing the viewer
      </div>
    </div>
  )
}

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

// Elapsed wall-clock for the running strip: "4.2s" under a minute, "2:41" after.
const fmtElapsed = (ms) => {
  if (ms == null) return null
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.floor((ms % 60000) / 1000)
  return `${m}:${String(s).padStart(2, '0')}`
}

// `?fixture=edit` (mock only) loads the synthetic edit fixture that exercises
// inserts + 3DFACEs + picking + the pending/version flow.
const fixtureParam = new URLSearchParams(window.location.search).get('fixture')

// `?demo=degraded` is a DEV-only demo hook to show the §10 degraded banner
// without a real APS_LIVE=1 fallback (degraded_mode is only true when a cloud
// run falls back; at APS_LIVE=0 it never trips). It only forces the banner's
// visibility — it fabricates no result numbers.
const demoDegraded = new URLSearchParams(window.location.search).get('demo') === 'degraded'

// `?ops=1` reveals the INTERNAL ops drawer (tenant kill-switch surface). Absent
// by default — the tenant-facing app never shows it.
const opsFlag = new URLSearchParams(window.location.search).get('ops') === '1'

// `?demo=locked` is a DEV-only hook that injects a synthetic single-writer
// checkout (held by another session) so the checkout chip + write-Run
// suppression can be exercised without touching the demo drawing's real
// manifest. It fabricates no result numbers — only a lock display.
const demoLocked = new URLSearchParams(window.location.search).get('demo') === 'locked'

// Engineering-only header controls (the Mock switch). The demo build is served
// from dist-demo with no backend, so a stray click on "Mock" in front of a cold
// audience points every call at the PROSPECT's own localhost:8130 — a dead port
// whose TypeError carries no .status, so the 401 auto-demo fallback never
// fires — and simultaneously reveals the Anthropic-credential panel. Keep the
// toggle for `npm run dev` and `?dev=1`; hide it on the demo build.
const devControls = (() => {
  try {
    if (import.meta.env?.DEV) return true
  } catch { /* no import.meta in a non-vite host */ }
  return new URLSearchParams(window.location.search).get('dev') === '1'
})()

// The run's dwg (intake source name) — unchanged from the original demo.
const DEFAULT_DRAWING_ID = 'rooftop_demo'

// Calm degraded copy for the agent tier (two-tier dispatch, wire §11). The
// deterministic path is never blocked by any of these — the banner just says
// so honestly. Keyed off classifyAgentError, never message text.
const agentBannerFor = (e) => {
  const kind = classifyAgentError(e)
  if (kind === 'quota') return { kind, message: 'AI paused — your built tools keep working.' }
  if (kind === 'grant') return { kind, message: 'Chat needs a linked Claude account.' }
  if (kind === 'entitlement') return { kind, message: 'Chat isn’t included in your plan — your built tools keep working.' }
  if (kind === 'busy') return { kind, message: 'The assistant is mid-turn — routed deterministically instead.' }
  if (kind === 'rate_limited') return { kind, message: 'AI rate-limited — routed deterministically; retry shortly.' }
  return { kind: 'unreachable', message: 'AI assistant unavailable — routed deterministically.' }
}

// The versioned-store drawing id the /versions + checkout reads key on. The
// write loop bootstraps the well-known `demo` drawing (write runs target it),
// so checkout/version reads default to `demo`. `?drawing=<id>` overrides it to
// drive a scratch drawing's checkout state through the real fetch path.
const CHECKOUT_DRAWING_ID =
  new URLSearchParams(window.location.search).get('drawing') || 'demo'

// Collapsible left-rail section (keeps the classic catalog reachable but
// secondary to the prompt box — the primary path).
function Section({ title, count, open, onToggle, children, innerRef, className = '' }) {
  return (
    <div className={`section ${className} ${open ? '' : 'collapsed'}`.replace(/\s+/g, ' ').trim()} ref={innerRef}>
      <button className="section-head" onClick={onToggle} aria-expanded={open}>
        <span>{title}{count != null ? <span className="n"> · {count}</span> : null}</span>
        <span className="chev">{open ? 'hide' : 'show'}</span>
      </button>
      {open && <div className="section-body">{children}</div>}
    </div>
  )
}

// Live-mode landing when there is no session: instead of a wall of red 401s with
// no way forward, a calm gate — sign-in for the live surface is coming; the demo
// is one click away. Shown only when a 401 was actually observed (not offline).
function plural(n, w) { return `${n} ${w}${n === 1 ? '' : 's'}` }

function SignedOutGate({ onDemo, onSignIn }) {
  return (
    <div className="card enter" style={{ margin: '0 0 16px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--foreground)' }}>You’re not signed in</div>
        <p className="panel-sub" style={{ margin: 0 }}>
          {onSignIn
            ? 'Sign in to load your tools and drawings from the cloud workspace, or explore the interactive demo on sample data.'
            : 'This is a live preview of Leaf against the cloud workspace. Sign-in for the live surface is coming soon — explore the interactive demo to try the prompt lanes, tool catalog, and viewer on a sample rooftop drawing.'}
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          {onSignIn && <button type="button" className="btn primary" onClick={onSignIn}>Sign in</button>}
          <button type="button" className={onSignIn ? 'btn ghost' : 'btn primary'} onClick={onDemo}>Explore the demo</button>
          <span className="dim" style={{ fontSize: 12 }}>No sign-in needed · sample data</span>
        </div>
      </div>
    </div>
  )
}


export default function App() {
  const [mock, setMock] = useState(config.mockDefault)
  const [intake, setIntake] = useState(null)
  const [versionIntake, setVersionIntake] = useState(null) // applied next-version
  const [loadErr, setLoadErr] = useState(null)
  const [intakeRetryKey, setIntakeRetryKey] = useState(0) // X3 Retry — bumping re-runs the intake load effect
  const [refreshFail, setRefreshFail] = useState(null) // {drawing_id, version} — post-write viewer refresh failed (X1)
  const [tools, setTools] = useState([])
  const [toolsErr, setToolsErr] = useState(null)
  const [toolsRetryKey, setToolsRetryKey] = useState(0) // R ladder / Retry chip — bumping re-runs the tools load effect
  const [visibleLayers, setVisibleLayers] = useState({})
  const [selectedTool, setSelectedTool] = useState(null)
  const [running, setRunning] = useState(false)
  const [runStatus, setRunStatus] = useState(null)   // live job phase: 'submitted' | 'running'
  const [runProgress, setRunProgress] = useState(null) // richer progress string (e.g. 'storing version')
  const [runElapsedMs, setRunElapsedMs] = useState(null) // ticking wall-clock while running
  const [result, setResult] = useState(null)
  const [runErr, setRunErr] = useState(null)
  const [selectedHandle, setSelectedHandle] = useState(null)
  const [pendingEdit, setPendingEdit] = useState(null)
  // Write-loop (§11, live mode): the current drawing/version chain from the last
  // drawing response and an undo/redo-in-flight guard. Version-completed events
  // surface as NT2 toasts (showToast), never as a persistent amber note.
  const [drawingState, setDrawingState] = useState(null) // {drawing_id, version, head, latest}
  const [versionBusy, setVersionBusy] = useState(false)  // undo/redo request in flight
  const [overlayStale, setOverlayStale] = useState(false) // last result overlay no longer matches shown version
  const [openTool, setOpenTool] = useState(null)         // the tool card expanded in ToolsPanel (for the write ghost)

  // --- platform session state ---
  const [tenant, setTenant] = useState(null)             // /api/session tenant echo (else "demo")
  const [tier, setTier] = useState(null)                 // real tier from the session echo (auth-live)
  const [org, setOrg] = useState(null)                   // org_id from the session echo (auth-live)
  const [catalog, setCatalog] = useState({ families: [], source: null }) // grouped families catalog
  const [catalogErr, setCatalogErr] = useState(null)     // families load failure (falls back to flat tools)
  const [openFamilies, setOpenFamilies] = useState({})   // per-family collapse state (family_id -> bool)
  const [usage, setUsage] = useState(null)               // GET /api/usage aggregate (live; null hides chip)
  const [usageAt, setUsageAt] = useState(0)              // ts of the last successful usage poll (freshness gate for quota self-clear)
  const [health, setHealth] = useState(null)             // GET /api/health (live; null -> static footer)
  // Real entitlements (GET /api/entitlements): drives the write-tool + build gates.
  // null in mock, or when the endpoint isn't deployed -> treated as full access.
  const [entitlements, setEntitlements] = useState(null) // {tier, entitlements:{run_read,run_write,build}, source}
  const [entLoading, setEntLoading] = useState(false)
  // Version-history browser (§ version chain) + read-only preview state.
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState(null)           // {drawing_id, head, latest, versions[]}
  const [historyErr, setHistoryErr] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [previewing, setPreviewing] = useState(null)     // {version} while previewing a non-head version
  const [previewIntake, setPreviewIntake] = useState(null) // that version's intake, seated read-only
  const [prompt, setPrompt] = useState('')               // dispatch box text
  const [route, setRoute] = useState(null)               // §12 nl-prompt routing decision
  const [routing, setRouting] = useState(false)          // awaiting the router
  const [routeErr, setRouteErr] = useState(null)         // routing call failed -> failed strip
  const [jobs, setJobs] = useState([])                   // live rail: recent GET /api/jobs
  const [authRequired, setAuthRequired] = useState(false) // live mode with no session: 401s observed -> polls stop, footer says so
  const is401 = (e) => e?.status === 401 || / -> 401$/.test(String(e?.message || ''))
  const [currentJobId, setCurrentJobId] = useState(null) // this session's live job id (dedupe)
  const [inflightPtr, setInflightPtr] = useState(null)   // localStorage re-attach pointer
  const [reattaching, setReattaching] = useState(false)  // auto re-attach in progress
  const [toolsOpen, setToolsOpen] = useState(false)      // left catalog collapsed by default
  const [authorOpen, setAuthorOpen] = useState(false)    // author flow (opens on build lane)
  const [authorSeed, setAuthorSeed] = useState('')       // build-lane prefill text
  const [authorSignal, setAuthorSignal] = useState(0)    // bump to re-seed the author flow

  // --- projects / orgs workspace (UI wave 2, item 1) ---
  const [orgId, setOrgId] = useState(getStoredOrgId())   // stored workspace org (localStorage leaf.org_id)
  const [projects, setProjects] = useState([])           // GET /api/projects
  const [projectsErr, setProjectsErr] = useState(null)   // platform unavailable (no DB) -> graceful note
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [openProjectId, setOpenProjectId] = useState(null)
  const [workspace, setWorkspace] = useState(null)       // hydration payload {project, drawing_versions[], jobs[], built_tools[]}
  const [canonicalVersionId, setCanonicalVersionId] = useState(null)
  const [wsLoading, setWsLoading] = useState(false)      // re-hydration in flight
  const [orgBusy, setOrgBusy] = useState(false)
  const [projectBusy, setProjectBusy] = useState(false)

  // --- single-writer checkout lock (item 3) ---
  const [checkout, setCheckout] = useState(null)         // {holder, acquired, expires} | null (from /versions)
  const [checkoutBusy, setCheckoutBusy] = useState(false) // take/release request in flight (3B)

  // --- ops drawer (item 2) ---
  const [opsDismissed, setOpsDismissed] = useState(false)

  // --- NT2 toast (one slot — newest replaces) + DT2 details drawer ---
  const [toast, setToast] = useState(null)   // {id, text, action?}
  const [drawer, setDrawer] = useState(null) // {title, rows[], action?, foot?}

  // --- Claude account grant (Concern 2 — the user's Claude login) ---
  // Kept strictly apart from the platform identity above (AUTH.md §0). The token
  // is write-only: we hold linkage status only, never the token itself.
  const [grant, setGrant] = useState(null)        // {linked, linked_at} | null (null = unknown/undeployed)
  const [grantLoading, setGrantLoading] = useState(false)
  const [grantBusy, setGrantBusy] = useState(false) // link/unlink request in flight
  const [grantErr, setGrantErr] = useState(null)
  const [claudeOpen, setClaudeOpen] = useState(false) // header Claude-account popover open
  // M5 guided tour: opened ONLY by the ?demo=tour (or ?demo=1) deep-link, and
  // only while mock is active. Exiting clears the tour and leaves you in mock.
  const [tourOn, setTourOn] = useState(() => {
    try { return shouldStartTour(typeof window !== 'undefined' ? window.location.search : '') } catch { return false }
  })
  const [tourLanded, setTourLanded] = useState(true) // did the current beat's real effect land?
  // Was this session deep-linked into the tour? Latched once so exiting the tour
  // still leaves a way back in (the tour re-enters at beat 1).
  const tourAvailable = useRef(false)
  if (tourOn) tourAvailable.current = true

  // --- agent tier (two-tier dispatch, wire §11; LIVE only — mock has no harness) ---
  const [agentMode, setAgentMode] = useState(null)  // null | 'race' (chip primary) | 'primary' (panel primary)
  const [agentBanner, setAgentBanner] = useState(null) // {kind, message} calm degraded note
  const {
    sessionId: agentSessionId,
    turns: agentTurns,
    startTurn: startAgentTurn,
    clear: clearAgentSession,
  } = useConverseSessionController({ drawingId: DEFAULT_DRAWING_ID, retryNotFound: true })

  const viewerRef = useRef(null)
  const authorSectionRef = useRef(null)
  const runningSinceRef = useRef(null)  // ms epoch the job entered 'running'
  const lastRunRef = useRef(null)       // {tool, params} for the retry affordance
  const barInputRef = useRef(null)      // ⌘K focuses the command bar input
  const resultBlockRef = useRef(null)   // toast "View" scroll target (result)
  const workspaceCardRef = useRef(null) // toast "View" scroll target (viewer)
  const toastSeqRef = useRef(0)         // monotonic toast ids
  const cannedSeq = useRef(0)           // supersedes an in-flight tour beat (typing + dispatch)
  const runSeqRef = useRef(0)           // Esc-interrupt bumps this to detach a run
  const runIntentSessionRef = useRef(null)
  if (!runIntentSessionRef.current) {
    const randomId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    runIntentSessionRef.current = `catalog-${randomId}`
  }
  const runIntentStateRef = useRef(null)
  if (!runIntentStateRef.current) {
    runIntentStateRef.current = createRunIntentState(runIntentSessionRef.current)
  }
  const runIntentSeqRef = useRef(0)

  const isEditFixture = mock && fixtureParam === 'edit'
  // What the panels/legend/selection reflect: a read-only version PREVIEW wins,
  // else the applied write-loop version, else the base intake.
  const shown = previewIntake || versionIntake || intake
  const projectName = shown?.dwg ? shown.dwg.split(/[\\/]/).pop().replace(/\.dwg$/i, '') : 'your project'
  // Honest identity: tenant id and tier are DISTINCT. tenant defaults to "demo"
  // off-auth; tier is only known when the session echo carries it (auth live).
  const tenantLabel = tenant || 'demo'
  const tierDisplay = tier || '—'
  // Entitlement tier prefers the policy read (authoritative) over the session echo.
  const entTier = entitlements?.tier || tier || 'demo'
  const gateTier = entTier

  // Real entitlement gates. Unknown capability (mock, or endpoint undeployed ->
  // entitlements null) resolves permissive (true) — byte-identical to today's
  // ungated demo. When a policy IS present, a false value genuinely disables the
  // affordance (the server enforces it too, so this only mirrors reality).
  const entOf = useCallback((key) => {
    const e = entitlements && entitlements.entitlements
    if (!e || typeof e[key] === 'undefined' || e[key] === null) return true
    return e[key] !== false
  }, [entitlements])
  const canRunWrite = entOf('run_write')
  const canBuild = entOf('build')
  // Agent tier gate: LIVE only (mock has no harness — behavior stays exactly
  // today's), and only when the plan doesn't explicitly exclude `converse`
  // (unknown/undeployed policy resolves permissive, like every other gate).
  const canConverse = entOf('converse')
  const agentDisabled = mock || !canConverse

  // Claude account (Concern 2) authoring gate. Only fires when we DEFINITELY know
  // the tenant has no linked Claude grant (live + grant read + linked === false).
  // Unknown linkage (mock, or the endpoint undeployed -> grant === null) never
  // gates — authoring stays byte-identical to today (template path unaffected).
  const claudeNotLinked = !mock && !!grant && grant.linked === false

  // --- single-writer checkout (item 3) ---
  // Our own holder id (best-effort): the echoed tenant, else the configured stub.
  // A checkout held by US is not a lock; only a checkout held by ANOTHER session
  // suppresses write-Run. `?demo=locked` injects a synthetic other-session lock.
  const ownHolder = tenant || config.tenant || 'demo-tenant'
  const rawCheckout = demoLocked
    ? { holder: 'another-session', acquired: new Date().toISOString(), expires: new Date(Date.now() + 3600e3).toISOString() }
    : checkout
  const otherHeldCheckout =
    (rawCheckout && rawCheckout.holder && rawCheckout.holder !== ownHolder) ? rawCheckout : null
  const writeLocked = !mock && !!otherHeldCheckout
  // We hold the single-writer lock (3B): our own holder id owns the checkout.
  // Not a lock on us (write-Run stays enabled) — it just offers a Release.
  const heldByUs = !mock && !!(rawCheckout && rawCheckout.holder && rawCheckout.holder === ownHolder)

  // Current open project's display name (from the hydration payload, else the list).
  const currentProjectName = openProjectId
    ? (workspace?.project?.name
        || projects.find((p) => (p.project_id || p.id) === openProjectId)?.name
        || null)
    : null

  // color-by-layer, stable across renders (keyed to base intake identity)
  const colorForLayer = useMemo(() => {
    const layers = intake?.layers || []
    const map = {}
    layers.forEach((l, i) => { map[l] = PALETTE[i % PALETTE.length] })
    return (layer) => map[layer] || '#9fb3c8'
  }, [intake])

  // load session (intake + tenant echo) + reset transient state on mode/fixture change
  useEffect(() => {
    let alive = true
    setIntake(null); setVersionIntake(null); setLoadErr(null); setRefreshFail(null)
    setResult(null); setSelectedHandle(null); setPendingEdit(null)
    setRunning(false); setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); setRunErr(null)
    setDrawingState(null); setVersionBusy(false)
    setOverlayStale(false); setOpenTool(null)
    setToast(null); setDrawer(null); setRouteErr(null)
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute(null); setRouting(false); setCurrentJobId(null); setTenant(null)
    setTier(null); setOrg(null)
    setHistoryOpen(false); setHistory(null); setHistoryErr(null)
    setPreviewing(null); setPreviewIntake(null)
    setAgentMode(null); setAgentBanner(null); clearAgentSession()
    runningSinceRef.current = null
    mockVersions.reset()
    const seat = (d) => {
      if (!alive) return
      setIntake(d)
      // MOCK write loop (M3): v1 of the 'demo' chain is the intake just seated,
      // so re-running the demo always starts from a clean v1.
      if (mock && !isEditFixture) mockVersions.seedBase(d)
      const vis = {}
      for (const l of d.layers || []) vis[l] = true
      setVisibleLayers(vis)
    }
    if (isEditFixture) {
      seat(editFixture) // synchronous local fixture — no backend
      return () => { alive = false }
    }
    getSession(mock)
      .then(({ intake: d, tenant: t, tier: ti, org: o }) => {
        if (!alive) return
        seat(d); setTenant(t); setTier(ti); setOrg(o)
      })
      .catch((e) => {
        if (!alive) return
        setLoadErr(humanizeError(e))
        if (!mock && is401(e)) {
          setAuthRequired(true)
          // Auto-fallback (B1): a VITE_MOCK=0 build that hits a 401 with Auth0
          // unconfigured can't sign in — flip to the demo instead of parking on
          // the gate, so the deployed link lands zero-click. SignedOutGate is
          // kept only for the authConfigured build (the user CAN sign in there).
          if (shouldAutoDemo({ authRequired: true, authConfigured, mock, signedIn: isSignedIn() })) setMock(true)
        }
      })
    return () => { alive = false }
  }, [mock, isEditFixture, intakeRetryKey])

  useEffect(() => {
    let alive = true
    setToolsErr(null)
    getTools(mock)
      .then((t) => alive && setTools(t))
      .catch((e) => alive && setToolsErr(humanizeError(e)))
    return () => { alive = false }
  }, [mock, toolsRetryKey])

  // Retry the tools load (ToolsPanel's Retry chip + the R ladder below).
  const retryTools = useCallback(() => setToolsRetryKey((k) => k + 1), [])

  // Families catalog (left rail): GET /api/capabilities grouped, or the mock
  // registry grouped client-side. Refetched on mode change and after authoring a
  // tool (so it lands in "Custom authored tools"). Falls back to the flat list
  // inside getCapabilities; a total failure surfaces catalogErr (flat ToolsPanel).
  const loadCatalog = useCallback(async () => {
    setCatalogErr(null)
    try {
      const cat = await getCapabilities(mock)
      setCatalog(cat)
      // default every family collapsed (keeps the left catalog secondary to the prompt)
      setOpenFamilies((prev) => {
        const next = { ...prev }
        for (const f of cat.families) if (!(f.family_id in next)) next[f.family_id] = false
        return next
      })
    } catch (e) {
      setCatalog({ families: [], source: null })
      setCatalogErr(humanizeError(e))
      if (!mock && is401(e)) setAuthRequired(true)
    }
  }, [mock])

  useEffect(() => { loadCatalog() }, [loadCatalog])

  // Auth0 return leg: if we came back from Universal Login (?code=&state=),
  // finish the exchange + store leaf.jwt, then reload so the fresh loads send
  // the token and land a 200 session (no gate). No-op in mock / when unconfigured.
  useEffect(() => {
    if (mock) return
    handleRedirectCallback().then((stored) => { if (stored) window.location.reload() })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Per-tenant spend chip: poll GET /api/usage on load (live only). null hides
  // the chip (mock, or the sibling endpoint not deployed yet) — no fake numbers.
  const loadUsage = useCallback(async () => {
    if (mock) { setUsage(null); return }
    try { setUsage(await getUsage()); setUsageAt(Date.now()) } catch { setUsage(null) }
  }, [mock])

  useEffect(() => { loadUsage() }, [loadUsage])

  // Real entitlements (live only): fetch on load so the write-tool + build gates
  // reflect the tenant's actual plan. null in mock (full access, all true) or when
  // the sibling endpoint isn't deployed yet (degrades to ungated — honest demo).
  const loadEntitlements = useCallback(async () => {
    if (mock) { setEntitlements(null); return }
    setEntLoading(true)
    try { setEntitlements(await getEntitlements()) } catch { setEntitlements(null) } finally { setEntLoading(false) }
  }, [mock])

  useEffect(() => { loadEntitlements() }, [loadEntitlements])

  // Real backend diagnostics for the footer chips (live only). Mock keeps the
  // static footer (setHealth(null)); any error -> null -> calm static fallback.
  const loadHealth = useCallback(async () => {
    if (mock) { setHealth(null); return }
    try { setHealth(await getHealth()) } catch { setHealth(null) }
  }, [mock])

  useEffect(() => { loadHealth() }, [loadHealth])

  // Claude account grant (Concern 2): read linkage status on load (live only).
  // null in mock (panel hidden) or when the sibling endpoint isn't deployed yet
  // (the affordance then degrades to today's ungated authoring — no gate, no
  // fabricated "linked" claim). Never fetches or holds the token itself.
  const loadGrant = useCallback(async () => {
    if (mock) { setGrant(null); return }
    setGrantLoading(true); setGrantErr(null)
    try { setGrant(await getClaudeGrant()) } catch { setGrant(null) } finally { setGrantLoading(false) }
  }, [mock])

  useEffect(() => { loadGrant() }, [loadGrant])

  const onLinkClaude = useCallback(async (token, kind) => {
    // token is passed straight to the API and never stored/logged here. `kind`
    // ("oauth" | "api_key") is the user's explicit choice; the server may echo the
    // authoritative kind back (it auto-detects), so prefer the response kind.
    setGrantBusy(true); setGrantErr(null)
    try {
      const res = await linkClaudeGrant(token, kind)
      setGrant({ linked: true, linked_at: res?.linked_at || new Date().toISOString(), kind: res?.kind || kind || null })
    } catch (e) {
      setGrantErr(humanizeError(e))
    } finally {
      setGrantBusy(false)
    }
  }, [])

  const onUnlinkClaude = useCallback(async () => {
    setGrantBusy(true); setGrantErr(null)
    try {
      await unlinkClaudeGrant()
      setGrant({ linked: false, linked_at: null })
    } catch (e) {
      setGrantErr(humanizeError(e))
    } finally {
      setGrantBusy(false)
    }
  }, [])

  // Projects workspace (item 1): fetch the org's projects (live only). No org
  // stored -> empty list + no error (the switcher offers "create workspace org").
  // Platform down (no DATABASE_URL / 500) -> projectsErr drives the graceful
  // "projects unavailable" note. Mock -> zero /api calls (no surface).
  const loadProjects = useCallback(async () => {
    if (mock || !orgId) { setProjects([]); setProjectsErr(null); return }
    setProjectsLoading(true); setProjectsErr(null)
    try {
      setProjects(await listProjects(orgId))
    } catch (e) {
      setProjects([]); setProjectsErr(humanizeError(e))
    } finally {
      setProjectsLoading(false)
    }
  }, [mock, orgId])

  useEffect(() => { loadProjects() }, [loadProjects])

  // Checkout lock (item 3): read the current drawing's version manifest and pick
  // up its `checkout` (sibling contract adds it to /versions). Live only; any
  // error -> null (no chip, no write-Run suppression). Refetched when the drawing
  // version changes and after runs (a write may acquire/release the lock).
  const loadCheckout = useCallback(async () => {
    if (mock) { setCheckout(null); return }
    const did = drawingState?.drawing_id || CHECKOUT_DRAWING_ID
    try {
      const v = await getDrawingVersions(mock, did)
      setCheckout(v?.checkout || null)
    } catch {
      setCheckout(null)
    }
  }, [mock, drawingState])

  useEffect(() => { loadCheckout() }, [loadCheckout])

  // Take / Release the single-writer checkout (3B). Both refetch /versions after
  // the call so the chip reflects the real lock (source of truth), and both stay
  // calm on failure — a 409 (someone else took it) / 403 (not the holder) just
  // means the refetched manifest shows the truth. Live only.
  const onTakeCheckout = useCallback(async () => {
    if (mock) return
    const did = drawingState?.drawing_id || CHECKOUT_DRAWING_ID
    setCheckoutBusy(true)
    try {
      await takeCheckout(did, ownHolder)
    } catch { /* calm — the refetch below shows the real state */ }
    finally {
      await loadCheckout()
      setCheckoutBusy(false)
    }
  }, [mock, drawingState, ownHolder, loadCheckout])

  const onReleaseCheckout = useCallback(async () => {
    if (mock) return
    const did = drawingState?.drawing_id || CHECKOUT_DRAWING_ID
    setCheckoutBusy(true)
    try {
      await releaseCheckout(did, ownHolder)
    } catch { /* calm — the refetch below shows the real state */ }
    finally {
      await loadCheckout()
      setCheckoutBusy(false)
    }
  }, [mock, drawingState, ownHolder, loadCheckout])

  // Tab-close reap beacon: on pagehide / tab-hidden, if a durable in-flight job
  // pointer exists, sendBeacon POST /api/jobs/{id}/close so the backend flags the
  // abandoned WorkItem closable (orphan reaper fails it) instead of billing until
  // the heartbeat window. Live only (mock has no server job). Idempotent
  // server-side; the localStorage re-attach path is untouched — if the user
  // returns quickly the re-attach still re-fetches the record (which may have
  // completed before the reaper swept). Absolute URL respects VITE_API_BASE.
  useEffect(() => {
    if (mock) return
    const fire = () => {
      const saved = readInflight()
      if (saved && saved.job_id) closeJobBeacon(saved.job_id)
    }
    const onHide = () => fire()
    const onVis = () => { if (document.visibilityState === 'hidden') fire() }
    window.addEventListener('pagehide', onHide)
    document.addEventListener('visibilitychange', onVis)
    return () => {
      window.removeEventListener('pagehide', onHide)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [mock])

  // Refresh the live job rail on demand (after a run completes, so the job shows
  // immediately rather than waiting for the next poll). No-op in mock.
  const refreshJobs = useCallback(async () => {
    if (mock) return
    try { setJobs(await listJobs()) } catch { /* transient */ }
  }, [mock])

  // Poll the recent-jobs list (live only; zero /api calls in mock). A 401 means
  // there is NO session — polling forever would just hammer the API with error
  // traffic, so the poll STOPS on the first 401 and resumes only when the mode
  // flips (the effect re-runs). Transient non-auth errors keep polling.
  useEffect(() => {
    if (mock) { setJobs([]); setAuthRequired(false); return }
    let alive = true
    let id = null
    const tick = async () => {
      try {
        const js = await listJobs()
        if (alive) { setJobs(js); setAuthRequired(false) }
      } catch (e) {
        if (e?.status === 401) {
          if (alive) setAuthRequired(true)
          if (id) { clearInterval(id); id = null }
        } /* other errors: transient, keep polling */
      }
    }
    tick()
    id = setInterval(tick, 2500)
    return () => { alive = false; if (id) clearInterval(id) }
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

  // --- NT2 toast plumbing (one slot — newest replaces; Toast auto-fades ~5s) --
  const showToast = useCallback((t) => {
    toastSeqRef.current += 1
    setToast({ id: toastSeqRef.current, ...t })
  }, [])
  const onToastDone = useCallback((id) => {
    setToast((cur) => (cur && cur.id === id ? null : cur))
  }, [])
  const viewResult = useCallback(() => {
    resultBlockRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [])
  const viewViewer = useCallback(() => {
    workspaceCardRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [])

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
    const seq = runSeqRef.current // Esc-interrupt / a new run bumps this to detach us
    const attached = () => alive && runSeqRef.current === seq
    ;(async () => {
      let rec
      try {
        rec = await getJob(saved.job_id)
      } catch {
        clearInflight(); setInflightPtr(null) // 404 / unreachable -> stale pointer
        return
      }
      if (!attached()) return
      setSelectedTool((t) => t || { name: saved.tool })
      setCurrentJobId(saved.job_id)
      if (rec.status === 'complete' || rec.status === 'failed') {
        setResult(recordToEnvelope(rec))
        if (rec.status === 'complete') {
          showToast({
            text: `${saved.tool || rec.tool || 'job'} complete`,
            action: { label: 'View', onClick: viewResult },
          })
        }
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
            if (!attached()) return
            setRunProgress(st.progress || null)
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
        if (attached()) {
          setResult(env)
          if (env?.ok) {
            showToast({
              text: `${saved.tool || env.tool || 'job'} complete`,
              action: { label: 'View', onClick: viewResult },
            })
          }
        }
      } catch (e) {
        if (attached()) setRunErr(humanizeError(e))
      } finally {
        if (attached()) {
          setRunning(false); setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
          setReattaching(false); setInflightPtr(null)
        }
        clearInflight()
        refreshJobs()
      }
    })()
    return () => { alive = false }
  }, [mock, markRunning, refreshJobs, showToast, viewResult])

  // Count ALL entity kinds per layer (polylines + inserts + 3DFACEs) so
  // insert/face-only layers (e.g. the ?fixture=edit Blocks/Surfaces layers)
  // stop reading 0 in the legend.
  const layerCounts = useMemo(() => {
    const c = {}
    for (const l of shown?.layers || []) c[l] = 0
    for (const pl of shown?.polylines || []) c[pl.layer] = (c[pl.layer] || 0) + 1
    for (const ins of shown?.inserts || []) c[ins.layer] = (c[ins.layer] || 0) + 1
    for (const f of shown?.faces3d || []) c[f.layer] = (c[f.layer] || 0) + 1
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

  // Swap the viewer + panels to a drawing version (§11). The completed event
  // ("Version 2 created" / "Reverted to version 1") fires the NT2 toast.
  const seatVersion = useCallback((view, drawingId, note) => {
    viewerRef.current?.applyVersion(view.intake)
    setVersionIntake(view.intake)
    setPendingEdit(null)
    setSelectedHandle(null)
    setDrawingState({ drawing_id: drawingId, version: view.head, head: view.head, latest: view.latest })
    if (note) showToast({ text: `${note} · ${drawingId}`, action: { label: 'View', onClick: viewViewer } })
    // the version chain changed — drop any open history popover / active preview.
    setHistoryOpen(false); setHistory(null); setPreviewing(null); setPreviewIntake(null)
  }, [showToast, viewViewer])

  const onUndo = useCallback(async () => {
    if (!drawingState || versionBusy) return
    setVersionBusy(true); setOverlayStale(true)
    try {
      const view = await undoDrawing(mock, drawingState.drawing_id)
      seatVersion(view, drawingState.drawing_id, `Reverted to version ${view.head}`)
    } catch (e) {
      setRunErr(humanizeError(e))
    } finally {
      setVersionBusy(false)
    }
  }, [mock, drawingState, versionBusy, seatVersion])

  const onRedo = useCallback(async () => {
    if (!drawingState || versionBusy) return
    setVersionBusy(true); setOverlayStale(true)
    try {
      const view = await redoDrawing(mock, drawingState.drawing_id)
      seatVersion(view, drawingState.drawing_id, `Advanced to version ${view.head}`)
    } catch (e) {
      setRunErr(humanizeError(e))
    } finally {
      setVersionBusy(false)
    }
  }, [mock, drawingState, versionBusy, seatVersion])

  // --- version-history browser + read-only preview -------------------------
  const toggleFamily = useCallback((id) => {
    setOpenFamilies((o) => ({ ...o, [id]: !o[id] }))
  }, [])

  // Shared by the History toggle and the X3 takeover's Retry chip.
  const loadHistory = useCallback(async () => {
    if (!drawingState) return
    setHistoryLoading(true); setHistoryErr(null)
    try {
      setHistory(await getDrawingVersions(mock, drawingState.drawing_id))
    } catch (e) {
      setHistoryErr(humanizeError(e)); setHistory(null)
    } finally {
      setHistoryLoading(false)
    }
  }, [mock, drawingState])

  const onToggleHistory = useCallback(async () => {
    if (historyOpen) { setHistoryOpen(false); return }
    setHistoryOpen(true)
    loadHistory()
  }, [historyOpen, loadHistory])

  // Read-only preview of a version. Head -> restore head + clear preview; any
  // other version -> seat that intake in the viewer WITHOUT touching head/latest
  // (undo/redo remain the only mutations; confirm via /intake?version=head).
  const onPreviewVersion = useCallback(async (v) => {
    if (!drawingState) return
    const isHead = v === drawingState.head
    try {
      const view = await getDrawingIntake(mock, drawingState.drawing_id, isHead ? 'head' : v)
      viewerRef.current?.applyVersion(view.intake)
      setSelectedHandle(null)
      if (isHead) {
        setVersionIntake(view.intake)   // re-sync the seated head
        setPreviewIntake(null); setPreviewing(null)
      } else {
        setPreviewIntake(view.intake)
        setPreviewing({ version: v })
        setOverlayStale(true)           // last run's overlay describes a different version
      }
    } catch (e) {
      setHistoryErr(humanizeError(e))
    }
  }, [mock, drawingState])

  const onBackToHead = useCallback(() => {
    if (drawingState) onPreviewVersion(drawingState.head)
  }, [drawingState, onPreviewVersion])

  // --- projects / orgs workspace handlers (item 1) -------------------------
  const onOpenProject = useCallback(async (pid) => {
    if (!pid) return
    setOpenProjectId(pid); setWorkspace(null); setCanonicalVersionId(null)
    setWsLoading(true); setProjectsErr(null)
    try {
      setWorkspace(await openProject(pid, orgId))
    } catch (e) {
      setWorkspace(null); setProjectsErr(humanizeError(e))
    } finally {
      setWsLoading(false)
    }
  }, [orgId])

  // Re-hydrate the open project (after a terminal run so jobs[] visibly grows).
  const rehydrate = useCallback(async () => {
    if (!openProjectId) return
    setWsLoading(true)
    try {
      setWorkspace(await openProject(openProjectId, orgId))
    } catch { /* transient — keep the last good hydration */ } finally {
      setWsLoading(false)
    }
  }, [openProjectId, orgId])

  const onCloseProject = useCallback(() => {
    setOpenProjectId(null); setWorkspace(null); setCanonicalVersionId(null)
  }, [])

  // Both creators accept the name from an inline F1 field (ProjectSwitcher);
  // the window.prompt fallback only fires when no name is passed (legacy path —
  // native dialogs are off-standard and slated for removal with the switcher).
  const onCreateOrg = useCallback(async (givenName) => {
    const name = typeof givenName === 'string'
      ? givenName
      : window.prompt('Name your workspace org', 'My workspace')
    if (name == null) return
    setOrgBusy(true); setProjectsErr(null)
    try {
      const org = await createOrg(name.trim() || 'My workspace')
      const id = org.org_id || org.id
      setStoredOrgId(id); setOrgId(id); setProjects([])
    } catch (e) {
      setProjectsErr(humanizeError(e))
    } finally {
      setOrgBusy(false)
    }
  }, [])

  const onCreateProject = useCallback(async (givenName) => {
    const name = typeof givenName === 'string'
      ? givenName
      : window.prompt('New project name', 'rooftop demo')
    if (name == null || !name.trim()) return
    setProjectBusy(true); setProjectsErr(null)
    try {
      const p = await createProject(name.trim(), orgId)
      setProjects((prev) => [...prev, p])
      await onOpenProject(p.project_id || p.id)
    } catch (e) {
      setProjectsErr(humanizeError(e))
    } finally {
      setProjectBusy(false)
    }
  }, [orgId, onOpenProject])

  const dismissRoute = useCallback(() => {
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute(null)
  }, [])

  const prepareRunParams = useCallback((tool, params) => {
    const isWrite = (tool.capabilities || []).includes('drawing.write')
    return selectedHandle
      ? { ...(params || {}), target_handle: selectedHandle, ...(isWrite ? { handle: selectedHandle } : {}) }
      : (params || {})
  }, [selectedHandle])

  const catalogRunContext = useMemo(() => createCatalogRunContext({
    tenantId: tenant || config.tenant,
    orgId,
    projectId: openProjectId || null,
    workspace,
    selectedVersionId: canonicalVersionId,
    drawingState,
    fallbackDrawingId: DEFAULT_DRAWING_ID,
  }), [tenant, orgId, openProjectId, workspace, canonicalVersionId, drawingState])
  const catalogRunContextRef = useRef(catalogRunContext)
  catalogRunContextRef.current = catalogRunContext

  const armDecision = useCallback((decision) => {
    if (decision?.lane !== 'run') {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      setRoute(decision)
      return decision
    }
    // Family catalog entries are presentation-normalized. Resolve the canonical
    // flat-catalog record before snapshotting so confirmation compares the same
    // server-sourced definition that RoutePanel will execute.
    const catalogTool = tools.find((candidate) => candidate.name === decision.tool)
    if (!catalogTool) {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      setRoute(decision)
      return decision
    }
    if (!mock && !tenant) return
    if (!catalogRunContextRef.current) {
      setRunErr('This workspace has no canonical drawing version to run. Import a drawing first.')
      return
    }
    const isWrite = (catalogTool.capabilities || []).includes('drawing.write')
    if (running || previewing || (isWrite && (writeLocked || !canRunWrite))) return
    const prepared = prepareRunParams(catalogTool, decision.params)
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `${runIntentSessionRef.current}:${++runIntentSeqRef.current}`,
      toolName: catalogTool.name,
      params: prepared,
      context: catalogRunContextRef.current,
      toolSnapshot: createCatalogToolSnapshot(catalogTool),
    })
    runIntentStateRef.current = staged.state
    setRouteErr(null)
    const armed = {
      ...decision,
      tool: catalogTool.name,
      params: staged.intent.params,
      runIntent: staged.intent,
    }
    setRoute(armed)
    return armed
  }, [tools, mock, tenant, prepareRunParams, running, previewing, writeLocked, canRunWrite, catalogRunContext])

  const onRequestCatalogRun = useCallback((tool, params, rationale = null) => {
    if (!tool) return
    return armDecision({
      lane: 'run', tool: tool.name, params, confidence: 1,
      rationale: rationale || 'Catalog selection. Confirm the exact tool and parameters before it runs.',
      alternatives: [],
    })
  }, [armDecision])

  const onRun = useCallback(async (tool, params, {
    intentConfirmed = false, runContext = null, idempotencyKey = null,
  } = {}) => {
    // Read-only version preview never mutates head — Run is disabled while previewing.
    if (previewing) return
    // Single-writer lock (item 3): write tools are suppressed while another
    // session holds the checkout; read tools are unaffected. Defensive guard —
    // the Run buttons for write tools are already disabled while locked.
    if (writeLocked && (tool.capabilities || []).includes('drawing.write')) return
    const seq = ++runSeqRef.current // Esc-interrupt detaches this run's handlers
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute(null); setRouteErr(null) // the decision strip is consumed by the run
    // Race tier (wire §11): taking the chip makes the deterministic run the
    // answer — stop RENDERING the agent stream. The turn itself may complete
    // server-side and stays persisted in the transcript; it is never cancelled.
    setAgentMode((m) => (m === 'race' ? null : m))
    setSelectedTool(tool)
    setRunning(true); setRunErr(null); setResult(null)
    setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
    setOverlayStale(false); setCurrentJobId(null); setRefreshFail(null)
    const merged = intentConfirmed ? params : prepareRunParams(tool, params)
    const executionContext = runContext || catalogRunContext
    lastRunRef.current = { tool, params: merged }
    // feed the picked entity to the tool so an edit tool can target it. A write
    // tool (delete-marked-panel) reads `handle`, so map the selection onto it
    // too — that makes the pending ghost's previewed deletion the real target.
    try {
      let env
      if (mock) {
        // Mock stays fully client-side — no jobs API, no progress phases.
        env = await runTool(mock, tool, merged, shown)
      } else {
        env = await runToolAsync(tool, merged, executionContext.drawingId, {
          // When a project is open, link the run so a platform Job row is recorded
          // (X-Org-Id + X-Project-Id) and the workspace jobs[] grows.
          orgId: executionContext.orgId || undefined,
          projectId: executionContext.projectId || undefined,
          dwgVersion: executionContext.drawingVersion ?? undefined,
          idempotencyKey: idempotencyKey || undefined,
          catalogDigest: (runContext?.toolSnapshot?.catalogDigest
            || createCatalogToolSnapshot(tool).catalogDigest || undefined),
          onSubmit: (job_id) => { saveInflight(job_id, tool.name); setCurrentJobId(job_id) },
          onStatus: (st) => {
            // Richer progress string (e.g. 'executing' · 'storing version' ·
            // 'extracting') when the backend emits it; null falls back to status.
            setRunProgress(st.progress || null)
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
      }
      if (runSeqRef.current !== seq) return // interrupted — the rail keeps the job
      setResult(env)
      // Completed event -> NT2 toast (bottom-center, quiet View action).
      if (env?.ok) {
        showToast({ text: `${tool.name} complete`, action: { label: 'View', onClick: viewResult } })
      }
      // Write loop (§11): a drawing.write run stamps result.new_version. Fetch
      // the fresh head intake and swap the viewer to the new version.
      if (!mock && env?.ok && env.result?.new_version) {
        const nv = env.result.new_version
        try {
          const view = await getDrawingIntake(mock, nv.drawing_id, 'head')
          seatVersion(view, nv.drawing_id, `Version ${nv.version} created`)
        } catch {
          // Completed act -> plain NT2 toast; the failed refresh surfaces as an
          // X1 red row at the viewer card (a failed act is never a toast).
          showToast({ text: `Version ${nv.version} created` })
          setRefreshFail({ drawing_id: nv.drawing_id, version: nv.version })
        }
      }
      // MOCK write loop (M3): the same beat, served by the in-memory chain —
      // commit v2 locally, then seat it through the identical seatVersion path
      // so Undo / Redo / History light up in the demo.
      if (mock && env?.ok && env.result?.new_version) {
        const nv = env.result.new_version
        // The engine's `new_version.version` is hardcoded to 2; only the chain
        // knows the real appended version, so a SECOND delete must read v3 here.
        let commit = null
        try {
          if (!mockVersions.isSeeded() && intake) mockVersions.seedBase(intake)
          commit = mockVersions.applyDelete(env.result.removed)
          const view = await getDrawingIntake(mock, nv.drawing_id, 'head')
          seatVersion(view, nv.drawing_id, `Version ${commit.version} created`)
        } catch {
          // Completed act -> plain NT2 toast; the failed refresh surfaces as an
          // X1 red row at the viewer card (a failed act is never a toast).
          showToast({ text: `Version ${commit?.version ?? nv.version} created` })
          setRefreshFail({ drawing_id: nv.drawing_id, version: commit?.version ?? nv.version })
        }
      }
    } catch (e) {
      if (runSeqRef.current === seq) setRunErr(humanizeError(e))
    } finally {
      if (runSeqRef.current === seq) {
        setRunning(false)
        setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
      }
      if (!mock) {
        clearInflight(); refreshJobs(); loadUsage(); loadCheckout()
        // re-hydrate the open project so its jobs[] reflects the just-finished run
        if (openProjectId) rehydrate()
      }
    }
  }, [mock, shown, intake, selectedHandle, markRunning, seatVersion, refreshJobs, previewing, loadUsage,
      loadCheckout, writeLocked, openProjectId, rehydrate, showToast, viewResult, prepareRunParams, catalogRunContext])

  const onConfirmCatalogRun = useCallback(async (intent, tool, params) => {
    let currentTool = tool
    let toolSnapshot
    try {
      if (!mock) {
        const latestTools = await getTools(false)
        currentTool = latestTools.find((candidate) => candidate.name === intent?.toolName) || null
      }
      toolSnapshot = createCatalogToolSnapshot(currentTool)
    } catch {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current, intent?.intentId)
      setRoute(null)
      setRunErr('That catalog tool changed or is no longer available. Choose Run again to create a new intent.')
      return
    }
    const confirmed = confirmRunIntent(runIntentStateRef.current, {
      intentId: intent?.intentId,
      sessionId: intent?.sessionId,
      toolName: currentTool?.name,
      params,
      context: catalogRunContextRef.current,
      toolSnapshot,
    })
    runIntentStateRef.current = confirmed.state
    if (!confirmed.ok) {
      setRoute(null)
      setRunErr('That run confirmation is no longer valid. Choose Run again to create a new intent.')
      return
    }
    onRun(currentTool, confirmed.execution.params, {
      intentConfirmed: true,
      runContext: { ...confirmed.execution.context, toolSnapshot: confirmed.execution.toolSnapshot },
      idempotencyKey: confirmed.execution.intentId,
    })
  }, [mock, onRun])

  // Retry the last run (plain affordance for retryable failures / transport hiccups).
  const onRetry = useCallback(() => {
    const last = lastRunRef.current
    if (last) onRequestCatalogRun(last.tool, last.params)
  }, [onRequestCatalogRun])

  // An agent-dispatched job (job_linked event) -> the SAME §7 attach
  // affordance the tab-close re-attach uses: subscribe to the job, stream
  // progress into the result pane, toast on completion. Never re-submits.
  const onAttachAgentJob = useCallback(async (jobId, toolName) => {
    if (!jobId || mock) return
    const seq = ++runSeqRef.current // Esc-interrupt detaches, like any run
    setSelectedTool({ name: toolName || 'job' })
    setCurrentJobId(jobId)
    setRunning(true); setRunErr(null); setResult(null)
    setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
    try {
      const env = await attachToJob(jobId, {
        onStatus: (st) => {
          if (runSeqRef.current !== seq) return
          setRunProgress(st.progress || null)
          if (st.status === 'running') markRunning()
          else setRunStatus(st.status || 'running')
        },
      })
      if (runSeqRef.current === seq) {
        setResult(env)
        if (env?.ok) {
          showToast({ text: `${toolName || env.tool || 'job'} complete`, action: { label: 'View', onClick: viewResult } })
        }
        // Agent-dispatched writes use the same immutable version contract as
        // catalog runs. Attaching must seat the new head, otherwise the receipt
        // says the write completed while the viewer still shows its parent.
        if (env?.ok && env.result?.new_version) {
          const nv = env.result.new_version
          try {
            const view = await getDrawingIntake(false, nv.drawing_id, 'head')
            seatVersion(view, nv.drawing_id, `Version ${nv.version} created`)
          } catch {
            showToast({ text: `Version ${nv.version} created` })
            setRefreshFail({ drawing_id: nv.drawing_id, version: nv.version })
          }
        }
      }
    } catch (e) {
      if (runSeqRef.current === seq) setRunErr(String(e.message || e))
    } finally {
      if (runSeqRef.current === seq) {
        setRunning(false)
        setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
      }
      refreshJobs()
    }
  }, [mock, markRunning, refreshJobs, seatVersion, showToast, viewResult])

  // X1 Retry for a failed post-write viewer refresh — re-fetch head and seat it.
  const onRetryViewerRefresh = useCallback(async () => {
    if (!refreshFail) return
    try {
      const view = await getDrawingIntake(mock, refreshFail.drawing_id, 'head')
      seatVersion(view, refreshFail.drawing_id, null)
      setRefreshFail(null)
    } catch { /* still failing — the X1 row stays */ }
  }, [mock, refreshFail, seatVersion])

  const onAuthor = useCallback(async (description) => {
    // R5 only stages bytes. It must not place a tool in the runnable catalog.
    return stageAuthorTool(mock, description)
  }, [mock])

  const onPublishAuthor = useCallback(async (staged) => {
    try {
      const res = await publishStagedAuthor(mock, staged)
      const tool = res.tool || staged.tool
      setTools((prev) => {
        const rest = prev.filter((t) => t.name !== tool.name)
        return [...rest, tool]
      })
      // Re-group the catalog so the new tool lands in "Custom authored tools"
      // (visible re-fetch of the grouped capabilities).
      loadCatalog()
      // Authoring is a ~1-2 min agent run — surface completion as an NT2 toast so
      // it is visible even when the author section is collapsed / scrolled away.
      showToast({
        text: `Tool published — ${tool.name}`,
        action: {
          label: 'View',
          onClick: () => {
            setAuthorOpen(true)
            setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
          },
        },
      })
      return { ...res, tool }
    } finally {
      setTourLanded(true)
    }
  }, [mock, loadCatalog, showToast])

  // "Run it now" from the author card — prefill the RUN lane (RoutePanel) with
  // the just-authored tool so the user confirms before it runs (paid actions
  // never auto-execute). The tool is already in `tools` (onAuthor added it), so
  // RoutePanel resolves it and shows a single Run.
  const onUseAuthored = useCallback((tool) => {
    if (!tool) return
    armDecision({
      lane: 'run', tool: tool.name, params: {}, confidence: 0.99,
      rationale: `Authored just now — confirm to run “${tool.name}”.`,
      alternatives: [],
    })
    setTimeout(() => document.querySelector('main')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
  }, [armDecision])

  // --- prompt-first dispatch (§12) -----------------------------------------
  // Live preview of which lane the text will route to (lights the hero's dots).
  const hintLane = useMemo(() => {
    const s = prompt.trim()
    if (!s) return null
    if (s.startsWith('/')) return 'run' // slash = explicit tool invocation
    try { return matchPrompt(s, tools).lane } catch { return null }
  }, [prompt, tools])

  // The slash-completable catalog: every runnable tool the CURRENT plan allows
  // (write tools drop out when the plan lacks run_write — the menu only offers
  // what the end user can actually complete and run).
  const slashTools = useMemo(
    () => tools.filter((t) => canRunWrite || !(t.capabilities || []).includes('drawing.write')),
    [tools, canRunWrite],
  )

  // `override` is an optional explicit string — the guided tour's canned prompt,
  // or the menu-picked "/tool" (state hasn't flushed yet). Click handlers pass
  // an event object, which is NOT a string, so the normal "dispatch what's in
  // the bar" path is untouched.
  const onDispatch = useCallback(async (override) => {
    const text = (typeof override === 'string' ? override : prompt).trim()
    if (!text || routing || running) return // no new decision while a run is in flight (Esc interrupts first)
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    // Slash fast-path: "/name" is an EXPLICIT invocation — no NL router call.
    // The route decision strip still asks for confirmation before anything runs.
    if (text.startsWith('/')) {
      const name = text.slice(1).split(/\s+/)[0]
      if (!name) return
      setRoute(null); setRouteErr(null)
      const t = tools.find((x) => (x.name || '').toLowerCase() === name.toLowerCase())
      if (t) {
        armDecision({
          lane: 'run', tool: t.name, params: {}, confidence: 1,
          rationale: `Explicit /${t.name} — you picked this tool.`,
          alternatives: [], slash: true,
        })
      } else {
        // Unknown name -> the resolver rows offer the nearest catalog matches:
        // substring hits first, else longest-common-prefix ≥ 3 (catches
        // trailing typos like /count-by-layre -> count-by-layer).
        const q = name.toLowerCase()
        const lcp = (a, b) => {
          let n = 0
          while (n < a.length && n < b.length && a[n] === b[n]) n++
          return n
        }
        const near = tools
          .map((x) => {
            const nm = (x.name || '').toLowerCase()
            return { x, score: nm.includes(q) ? 1000 + q.length : lcp(nm, q) }
          })
          .filter((s) => s.score >= 3)
          .sort((a, b) => b.score - a.score)
          .slice(0, 3)
          .map((s) => ({ tool: s.x.name, description: s.x.description }))
        setRoute({ lane: 'run', tool: name, params: {}, confidence: 0, alternatives: near, slash: true })
      }
      return
    }
    setRouting(true); setRoute(null); setRouteErr(null)
    try {
      const r = await nlPrompt(mock, text, tools)
      // Two-tier dispatch (wire contract §11). Tier 1 (the deterministic §12
      // classifier above) never changes; the agent tier is ADDITIVE and only
      // exists in live mode with the converse entitlement (agentDisabled).
      const conf = Number(r.confidence) || 0
      const openAuthorFlow = () => {
        setAuthorSeed(text)
        setAuthorSignal((n) => n + 1)
        setAuthorOpen(true)
        // bring the (left-rail) author flow into view
        setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
      }
      const chipOnly = agentDisabled ||
        (r.lane === 'run' && !!r.tool && conf >= THRESHOLDS.CHIP_ONLY)
      if (chipOnly) {
        // Today's behavior verbatim (also the whole story when the agent tier
        // is disabled: mock, or a plan without converse).
        armDecision(r)
        if (r.lane === 'build') openAuthorFlow()
      } else {
        const hint = { lane: r.lane, tool: r.tool || null, confidence: conf, rationale: r.rationale || null }
        setAgentBanner(null)
        if (r.lane === 'run' && !!r.tool && conf >= THRESHOLDS.RACE_MIN) {
          // RACE band: today's chip stays primary AND an agent turn starts
          // alongside. A failed agent start never degrades the chip.
          armDecision(r)
          try {
            await startAgentTurn(text, hint)
            setAgentMode('race')
          } catch (e) {
            setAgentBanner(agentBannerFor(e))
          }
        } else {
          // AGENT primary: low confidence, build/solve lane, or no match.
          try {
            await startAgentTurn(text, hint)
            setAgentMode('primary')
          } catch (e) {
            // Degraded fallback: EXACTLY today's Tier-1 rendering + calm banner.
            armDecision(r)
            if (r.lane === 'build') openAuthorFlow()
            setAgentBanner(agentBannerFor(e))
          }
        }
      }
      // main made onDispatch return the routed decision to its callers; the
      // agent tier above is additive, so the return contract is preserved.
      return r
    } catch (e) {
      // A failed routing call is a FAILED act — it rides the red strip above
      // the well (with Retry + its key), never a fake confidence-0 route card.
      setRouteErr(humanizeError(e))
    } finally {
      setRouting(false)
    }
  }, [prompt, routing, running, mock, tools, agentDisabled, startAgentTurn, armDecision])

  // Typing invalidates a shown route/failure — the decision must match the text.
  const onPromptChange = useCallback((v) => {
    setPrompt(v)
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute((r) => (r ? null : r))
    setRouteErr((e) => (e ? null : e))
  }, [])

  // Pick an alternative from a low-confidence / live-only route -> a user-picked
  // (high-confidence) run route for that capability.
  const onPickAlternative = useCallback((name) => {
    const prev = route
    armDecision({
      lane: 'run', tool: name, params: {}, confidence: 0.99,
      rationale: 'You picked this capability from the alternatives.',
      alternatives: (prev?.alternatives || []).filter((a) => a.tool !== name),
      stub: prev?.stub, stubReason: prev?.stubReason,
    })
  }, [armDecision, route])

  const onOpenAuthor = useCallback(() => {
    setAuthorOpen(true)
    setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
  }, [])

  // --- M5 guided tour: canned prompts ride the REAL handlers ----------------
  // The tour types its beat into the real command bar, dispatches it through the
  // real nl-prompt router (onDispatch), and — for a read-only run beat — runs it
  // through the same guarded intent path. NOTHING here fabricates a result.
  // Write beats (the versioned delete) deliberately stop at the confirm card;
  // paid/destructive actions never auto-execute, tour or not.
  const onCannedPrompt = useCallback(async (text, step) => {
    if (!text) return
    // Cancellation token: Exit/Skip must stop the bar mid-character instead of
    // typing on after the tour is gone, and a rapid Back must supersede the
    // in-flight beat rather than interleave two typing loops.
    const seq = (cannedSeq.current += 1)
    setTourLanded(false)
    // self-type into the real bar so the audience sees the sentence being written
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute(null); setRouteErr(null)
    for (let i = 1; i <= text.length; i += 1) {
      if (cannedSeq.current !== seq) return
      setPrompt(text.slice(0, i))
      // eslint-disable-next-line no-await-in-loop
      await new Promise((res) => setTimeout(res, 22))
    }
    if (cannedSeq.current !== seq) return
    let r = null
    try {
      r = await onDispatch(text)
      if (cannedSeq.current !== seq) return
      if (r && r.lane === 'run' && step?.action === 'run') {
        const toolObj = tools.find((t) => t.name === r.tool)
        const isWrite = (toolObj?.capabilities || []).includes('drawing.write')
        if (toolObj && !isWrite) {
          onRequestCatalogRun(toolObj, r.params || {}, 'Guided tour selection. Confirm before it runs.')
        }
      }
    } finally {
      // The BUILD lane hands off to AuthorPanel's auto-submit, whose onAuthor
      // finally owns `landed` — flipping it here would unlock Next before the
      // tool is actually authored, which is the whole differentiator beat.
      if (cannedSeq.current === seq && !(r && r.lane === 'build')) setTourLanded(true)
    }
  }, [onDispatch, onRequestCatalogRun, tools])

  const onTourExit = useCallback(() => {
    // Leaving the tour keeps you exactly where you are — in mock, on the same
    // drawing, with your last real result on screen.
    cannedSeq.current += 1   // kills any in-flight typing / dispatch
    setTourOn(false)
    setTourLanded(true)
  }, [])

  // Click a terminal job in the rail -> open its DT2 provenance drawer (over
  // the rail — the center pane's current result stays untouched; "Show in
  // result pane" is the drawer's one quiet action).
  const onSelectJob = useCallback(async (job) => {
    if (!job || (job.status !== 'complete' && job.status !== 'failed')) return
    try {
      const rec = await getJob(job.job_id)
      const env = recordToEnvelope(rec)
      const rows = [
        `job ${job.job_id}`,
        `tool ${rec.tool || job.tool || '—'}`,
        `status ${rec.status}`,
        // On the mock path the engine's new_version.version is a hardcoded 2 —
        // the chain owns the real head, so read it there.
        `version ${env?.result?.new_version
          ? (mockVersions.isSeeded() ? mockVersions.list().head : env.result.new_version.version)
          : (env?.version ?? '—')}`,
        `timing ${rec.elapsed_ms != null ? `${rec.elapsed_ms} ms` : (env?.timing_ms != null ? `${env.timing_ms} ms` : '—')}`,
        `cost ${env?.cost && env.cost.usd_est != null ? `$${Number(env.cost.usd_est).toFixed(4)}` : '—'}`,
        `degraded ${(env?.degraded_mode || rec.degraded_mode) ? 'yes — local fallback' : 'no'}`,
      ]
      // A mock envelope can carry a bare string error — don't render a blank row.
      if (env?.error) rows.push(typeof env.error === 'string'
        ? `error ${env.error}`
        : `error ${env.error.error_code || ''} · ${env.error.message || ''}`)
      setDrawer({
        title: `${rec.tool || job.tool || 'job'} · provenance`,
        rows,
        action: {
          label: 'Show in result pane',
          onClick: () => {
            setSelectedTool({ name: rec.tool || job.tool })
            setResult(env)
            setRunErr(null); setRunning(false); setOverlayStale(false)
            setCurrentJobId(job.job_id)
            setDrawer(null)
            setTimeout(() => resultBlockRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
          },
        },
        foot: 'Esc closes — the rail behind never re-flows.',
      })
    } catch (e) {
      setRunErr(humanizeError(e))
    }
  }, [])

  // "Details" in the result receipt area -> the run's DT2 provenance drawer.
  const openRunDetails = useCallback(() => {
    if (!result) return
    const env = result
    const rows = [
      `job ${currentJobId || '—'}`,
      `tool ${env.tool || selectedTool?.name || '—'}`,
      // Mock path: the engine hardcodes new_version.version to 2; the chain
      // (seeded only in mock) owns the real head.
      `version ${env.result?.new_version
        ? (mockVersions.isSeeded() ? mockVersions.list().head : env.result.new_version.version)
        : (env.version ?? '—')}`,
      `timing ${env.timing_ms != null ? `${env.timing_ms} ms` : '—'}`,
      `cost ${env.cost && env.cost.usd_est != null ? `$${Number(env.cost.usd_est).toFixed(4)}` : '—'}`,
      `degraded ${env.degraded_mode ? 'yes — local fallback' : 'no'}`,
    ]
    if (env.error) rows.push(typeof env.error === 'string'
      ? `error ${env.error}`
      : `error ${env.error.error_code || ''} · ${env.error.message || ''}`)
    setDrawer({
      title: 'Run · provenance',
      rows,
      action: currentJobId
        ? { label: 'Copy job id', onClick: () => navigator.clipboard?.writeText(String(currentJobId)) }
        : null,
      foot: 'Esc closes — provenance is read-only.',
    })
  }, [result, currentJobId, selectedTool])

  // Header "Details" -> session identity/spend drawer (metadata demoted from
  // the permanent header chrome per the standard).
  const openSessionDetails = useCallback(() => {
    const rows = [
      `org ${org || '—'}`,
      `tenant ${tenantLabel} · tier ${tierDisplay}`,
      `mode ${mock ? 'mock (no cloud)' : `live · ${config.apiBase}`}`,
      `entitlement tier ${gateTier}`,
    ]
    if (!mock && usage) {
      rows.push(`spend $${Number(usage.today?.usd_est || 0).toFixed(3)} today · ${usage.today?.runs || 0} run${(usage.today?.runs || 0) === 1 ? '' : 's'}`)
      if (usage.cap?.enabled && typeof usage.cap?.remaining === 'number') {
        rows.push(`cap $${Number(usage.cap.remaining).toFixed(2)} left`)
      }
    }
    rows.push(`build ${__BUILD_HASH__}`)
    setDrawer({
      title: 'Session · provenance',
      rows,
      action: isSignedIn()
        ? { label: 'Sign out', onClick: logout }
        : { label: 'Refresh', onClick: () => { loadUsage(); loadHealth() } },
      foot: 'Your account and usage.',
    })
  }, [org, tenantLabel, tierDisplay, mock, usage, gateTier, loadUsage, loadHealth])

  // Esc while a live run is in flight: detach this session from the job (the
  // rail keeps tracking it; the close beacon flags it reap-able server-side).
  const interruptRun = useCallback(() => {
    runSeqRef.current += 1
    if (!mock && currentJobId) closeJobBeacon(currentJobId)
    clearInflight(); setInflightPtr(null); setReattaching(false)
    setRunning(false); setRunStatus(null); setRunProgress(null); setRunElapsedMs(null)
    runningSinceRef.current = null
    refreshJobs()
  }, [mock, currentJobId, refreshJobs])

  // R ladder (item D): every displayed R keycap must be live, and only the
  // HIGHEST-PRIORITY visible error responds — one keypress, one retry, never
  // two. ResultPanel owns R for run/result errors via its own window listener
  // (rung 1 — the global ladder stands down for it); the rungs below cover the
  // routing strip, the history takeover, the tools-catalog row, the families
  // row, and the post-write refresh row. Rows render their R keycap only when
  // they are the active rung (rTarget), so a shown cap is never inert.
  const anyFamilyOpen = useMemo(
    () => catalog.families.some((f) => openFamilies[f.family_id]),
    [catalog, openFamilies],
  )
  // Mirrors ResultPanel's own canRetryKey condition (its listener is authoritative).
  const resultOwnsR = !running && (!!runErr ||
    !!(result && result.error && !result.entitlement_required && result.error.retryable))
  const rTarget = useMemo(() => {
    if (running) return null
    if (resultOwnsR) return 'result' // ResultPanel's listener handles it
    if (routeErr) return 'route'
    if (historyOpen && historyErr && !historyLoading) return 'history'
    if (toolsErr && (catalogErr ? toolsOpen : anyFamilyOpen)) return 'tools'
    if (catalogErr && !(!mock && authRequired)) return 'catalog'
    if (refreshFail) return 'refresh'
    return null
  }, [running, resultOwnsR, routeErr, historyOpen, historyErr, historyLoading,
      toolsErr, catalogErr, toolsOpen, anyFamilyOpen, mock, authRequired, refreshFail])

  // Global key ladder: ⌘K summons the bar; Esc closes the topmost surface
  // (drawer > history > route/failed strip > running run > selection > open
  // project); R retries the highest-priority visible error (outside text
  // inputs); any OTHER bare printable keystroke falls into the prompt bar
  // (type-to-fall-through).
  useEffect(() => {
    const onKey = (e) => {
      const tag = ((e.target && e.target.tagName) || '').toLowerCase()
      const typing = tag === 'input' || tag === 'textarea'
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        barInputRef.current?.focus()
        return
      }
      if (e.key === 'Escape') {
        if (drawer) { setDrawer(null); return }
        if (historyOpen) { setHistoryOpen(false); return }
        if (route) { dismissRoute(); return }
        if (routeErr || runErr) { setRouteErr(null); setRunErr(null); return }
        if (running) { interruptRun(); return }
        if (selectedHandle) { setSelectedHandle(null); return }
        // Bottom rung: the WorkspaceSummary Esc cap — close the open project
        // only once every higher surface has already yielded.
        if (openProjectId) { onCloseProject() }
        return
      }
      // R: fire the ladder's active rung. rTarget === 'result' means
      // ResultPanel's own listener owns the keypress (duplicating it here
      // double-fired the retry: two POST /api/run from one keypress).
      if (!typing && (e.key === 'r' || e.key === 'R') &&
          !e.metaKey && !e.ctrlKey && !e.altKey &&
          rTarget && rTarget !== 'result') {
        e.preventDefault()
        if (rTarget === 'route') onDispatch()
        else if (rTarget === 'history') loadHistory()
        else if (rTarget === 'tools') retryTools()
        else if (rTarget === 'catalog') loadCatalog()
        else if (rTarget === 'refresh') onRetryViewerRefresh()
        return
      }
      // Type-to-fall-through (operator rule): a bare printable keystroke on the
      // surface always falls into the prompt bar. Focus BEFORE the default
      // action so the character itself lands in the input; visible mnemonic
      // rungs above (⌘K, Esc, R-on-failed-strip) keep priority. Never steals
      // from an editable element.
      const editable = typing || tag === 'select' || (e.target && e.target.isContentEditable)
      // A focused interactive control keeps its keys (Space must ACTIVATE a
      // button, not yank focus); Space never falls through; overlays (drawer,
      // history) keep typing local to themselves.
      const interactive = e.target instanceof Element &&
        e.target.closest('button, a, summary, [role="button"], [role="option"], [role="menuitem"]')
      if (!editable && !interactive && !drawer && !historyOpen &&
          !e.metaKey && !e.ctrlKey && !e.altKey && e.key.length === 1 && e.key !== ' ') {
        barInputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawer, historyOpen, route, routeErr, runErr, running, selectedHandle,
      interruptRun, onDispatch, openProjectId, onCloseProject, rTarget,
      loadHistory, retryTools, loadCatalog, onRetryViewerRefresh, dismissRoute])

  // Click-to-fall-through (operator rule): a click anywhere on the surface that
  // doesn't otherwise take an action activates the prompt bar. Real
  // interactions (buttons, links, fields, the viewer canvas — a click there
  // selects an entity) and popover/drawer interiors keep their clicks, and an
  // in-flight text selection is never stolen from.
  useEffect(() => {
    const onClick = (e) => {
      const t = e.target
      if (!(t instanceof Element)) return
      if (t.closest('button, a, input, textarea, select, label, summary, canvas, '
        + '[role="button"], [role="option"], [role="listbox"], [contenteditable="true"], '
        + '.drawer, .vh-pop, .proj-menu, .claude-pop, .ops-drawer, .resolver, .bar')) return
      const sel = window.getSelection && window.getSelection()
      if (sel && sel.toString()) return
      barInputRef.current?.focus()
    }
    window.addEventListener('click', onClick)
    return () => window.removeEventListener('click', onClick)
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
      // Prefer the richer progress string ('storing version' etc.) when present.
      progress: runProgress || runStatus || 'running',
      elapsed_ms: runElapsedMs != null ? runElapsedMs : (result?.timing_ms ?? null),
      degraded_mode: !!result?.degraded_mode,
      error: result?.error || null,
      // Per-run cost (live APS) for the rail card; null for mock runs.
      cost: result?.cost || null,
      // Plan-boundary rejection -> calm amber rail card (not red failed).
      entitlement_required: !!result?.entitlement_required,
    }
  }, [selectedTool, running, runStatus, runProgress, result, runElapsedMs, currentJobId])

  const degraded = !!result?.degraded_mode || demoDegraded

  // Total capabilities across families (footer + fam-title, honest count).
  const capCount = useMemo(
    () => catalog.families.reduce((n, f) => n + f.capabilities.length, 0),
    [catalog],
  )

  // A run rejected by the hard SPEND cap (§ broker 402) -> calm amber quota card,
  // not a red failure. The backend's message is authoritative. The coarse DAILY
  // run-count limit (429) shares the quota_exceeded code but carries a distinct
  // `quota_kind` — it renders its OWN card below, so exclude it here.
  const quotaError = (result && !result.ok && result.error &&
    result.error.error_code === 'quota_exceeded' && result.quota_kind !== 'daily_runs')
    ? result.error : null

  // A run rejected by the coarse per-tenant DAILY run limit (HTTP 429) -> calm
  // amber "daily limit reached" card (distinct from the spend cap). Nothing ran.
  const runQuotaError = (result && !result.ok && result.quota_kind === 'daily_runs')
    ? result : null

  // NT2 self-clearing: an ongoing quota condition derives from LIVE usage, not
  // the envelope that first raised it. While one shows, re-poll GET /api/usage
  // every 60s; the banner clears itself only when a poll STRICTLY FRESHER than
  // the condition shows headroom again (cap raised / day rolled). It is never
  // user-dismissed, and a stale pre-error poll can never suppress first paint
  // (suppression requires quotaAt > 0 AND usageAt > quotaAt).
  const [quotaAt, setQuotaAt] = useState(0)
  useEffect(() => {
    setQuotaAt(quotaError || runQuotaError ? Date.now() : 0)
  }, [quotaError, runQuotaError])
  useEffect(() => {
    if (mock || (!quotaError && !runQuotaError)) return undefined
    loadUsage()
    const id = setInterval(loadUsage, 60_000)
    return () => clearInterval(id)
  }, [mock, quotaError, runQuotaError, loadUsage])
  const freshUsage = quotaAt > 0 && usageAt > quotaAt ? usage : null
  const spendCapCleared = !!(quotaError && freshUsage?.cap?.enabled &&
    typeof freshUsage.cap.remaining === 'number' && freshUsage.cap.remaining > 0)
  const dailyRunsCleared = !!(runQuotaError && freshUsage?.today &&
    Number.isFinite(Number(runQuotaError.limit)) &&
    Number(freshUsage.today.runs || 0) < Number(runQuotaError.limit))
  const quotaShown = quotaError && !spendCapCleared ? quotaError : null
  const runQuotaShown = runQuotaError && !dailyRunsCleared ? runQuotaError : null

  // A run rejected by a plan boundary (HTTP 403 entitlement_required) -> calm
  // amber plan notice, also not a red failure. Nothing ran / was billed.
  const entitlementError = (result && result.entitlement_required) ? result : null

  // NR: the active ongoing conditions, docked at the result pane. Two or more
  // collapse to ONE line with a count instead of stacking banners.
  // live, no session -> calm gate, hush the 401 red. A 401 on a SIGNED-IN
  // session of an auth-unconfigured build is different: the token was rejected
  // and there is no way to re-auth, so it is a real failure — fall through to
  // the pane-fail surface (Retry + Back to the demo), never the inert overlay
  // (round-2 review F1: that state was an unrecoverable blank).
  const signedOut = !mock && authRequired && (authConfigured || !isSignedIn())

  const advisories = [
    quotaShown && 'spend cap',
    runQuotaShown && 'daily limit',
    entitlementError && 'plan',
    degraded && 'local fallback',
  ].filter(Boolean)

  // Publish the MEASURED chrome heights that structural.css's drawer offsets
  // read. The header wraps on a phone (~95px, not the hardcoded 49px fallback),
  // so without this the DT2 drawer paints over the header; on desktop it fixes a
  // latent 7px gap under a 42px header. ResizeObserver keeps it live on rotate.
  useLayoutEffect(() => {
    const h = document.querySelector('header.top')
    const f = document.querySelector('footer.foot-bar')
    if (!h || !f) return undefined
    const sync = () => {
      const r = document.documentElement.style
      r.setProperty('--drawer-top', `${Math.round(h.getBoundingClientRect().height)}px`)
      r.setProperty('--drawer-bottom', `${Math.round(f.getBoundingClientRect().height)}px`)
    }
    sync()
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(sync)
    ro.observe(h); ro.observe(f)
    return () => ro.disconnect()
  }, [])

  // M1 exit fades for the parent-mounted panels: hold the mount through the
  // 180 ms .exit fade (useExit follows Toast.jsx's pattern).
  const historyExit = useExit(historyOpen)
  // The internal ops / tenant kill-switch drawer must never be reachable from a
  // public demo build — `?ops=1` is a no-op in mock.
  const opsExit = useExit(opsFlag && !mock && !opsDismissed)

  return (
    <div className="app">
      <header className="top">
        <div className="mark"><span className="diamond" aria-hidden="true" /> Leaf — build CAD tools with AI</div>
        <div className="proj">
          <ProjectSwitcher
            mock={mock}
            projectName={projectName}
            orgId={orgId}
            projects={projects}
            openProjectId={openProjectId}
            currentName={currentProjectName}
            unavailable={projectsErr}
            loading={projectsLoading}
            orgBusy={orgBusy}
            projectBusy={projectBusy}
            onCreateOrg={onCreateOrg}
            onCreateProject={onCreateProject}
            onOpenProject={onOpenProject}
          />
          <span className="meta">
            {shown ? `${shown.polylines.length} polylines · ${shown.layers.length} layers` : 'loading'}
          </span>
          {mock && <span className="tag amber">Demo</span>}
        </div>
        <div className="spacer" />
        <div className="who">
          {/* Header metadata (org · tenant · tier · spend · API base) is demoted
              behind Details -> the DT2 session drawer, per the standard. */}
          <button type="button" className="chip-act" onClick={openSessionDetails}>Details</button>
          {devControls && (
            <label className="switch">
              <input
                type="checkbox"
                checked={mock}
                disabled={tourOn}
                onChange={(e) => setMock(e.target.checked)}
                aria-label="Use mock data (off = live backend)"
              />
              <span>Mock</span>
            </label>
          )}
          {/* Live-only chrome (Claude-account terminal panel) is hidden in the
              demo — it can't work signed-out. Guarded on !mock. */}
          {!mock && (
            <ClaudeAccountPanel
              mock={mock}
              grant={grant}
              loading={grantLoading}
              busy={grantBusy}
              error={grantErr}
              open={claudeOpen}
              onToggle={setClaudeOpen}
              onLink={onLinkClaude}
              onUnlink={onUnlinkClaude}
            />
          )}
        </div>
      </header>

      <aside className="nav">
        <div className="fam-title">
          Catalog · {catalog.families.length} famil{catalog.families.length === 1 ? 'y' : 'ies'} · {capCount} caps
          {catalog.source === 'flat-fallback' ? ' · flat' : ''}
        </div>
        {catalogErr && !signedOut && (
          <>
            <div className="inline-error" style={{ margin: '0 4px 4px' }}>
              Couldn’t load families: {catalogErr}
              <button type="button" className="chip-act" onClick={loadCatalog}>Retry</button>
              {rTarget === 'catalog' && <span className="key" aria-hidden="true">R</span>}
            </div>
            <div className="dim" style={{ margin: '0 4px 8px', fontSize: 11.5 }}>Showing the flat tool list instead.</div>
            <Section title="Tools" count={tools.length} open={toolsOpen} onToggle={() => setToolsOpen((o) => !o)}>
              <ToolsPanel
                tools={tools}
                writeLocked={writeLocked}
                writeEntitled={canRunWrite}
                error={toolsErr}
                onRetry={retryTools}
                retryKey={rTarget === 'tools'}
                running={running || !!previewing}
                selectedTool={selectedTool}
                onRequestRun={onRequestCatalogRun}
                onOpenTool={setOpenTool}
              />
            </Section>
          </>
        )}
        {!catalogErr && catalog.families.length === 0 && (
          // Loading = static content-shaped skeleton rows (no spinner, no text note).
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}
        {catalog.families.map((fam) => (
          <Section
            key={fam.family_id}
            title={fam.label}
            count={fam.capabilities.length}
            open={!!openFamilies[fam.family_id]}
            onToggle={() => toggleFamily(fam.family_id)}
          >
            <ToolsPanel
              tools={fam.capabilities}
              writeLocked={writeLocked}
              writeEntitled={canRunWrite}
              subtitle={fam.description}
              error={toolsErr}
              onRetry={retryTools}
              retryKey={rTarget === 'tools'}
              running={running || !!previewing}
              selectedTool={selectedTool}
              onRequestRun={onRequestCatalogRun}
              onOpenTool={setOpenTool}
            />
          </Section>
        ))}
        <Section
          title="Author a tool"
          className="author-section"
          open={authorOpen}
          onToggle={() => setAuthorOpen((o) => !o)}
          innerRef={authorSectionRef}
        >
          <AuthorPanel
            onAuthor={onAuthor}
            onPublish={onPublishAuthor}
            onUseAuthored={onUseAuthored}
            seed={authorSeed}
            seedSignal={authorSignal}
            seedAutoSubmit={tourOn}
            notLinked={claudeNotLinked}
            onLinkClaude={() => setClaudeOpen(true)}
            buildEntitled={canBuild}
          />
        </Section>
      </aside>

      <div className="center-col">
        <main className="center-scroll">
        {/* the tour carries its own persistent banner — don't stack two */}
        {mock && !tourOn && <DemoBanner />}
        {/* There is a way back IN: leaving the tour (Skip / Exit) used to be
            one-way, with a hard reload the only re-entry — forbidden on stage. */}
        {mock && !tourOn && tourAvailable.current && (
          <button type="button" className="chip-neutral" onClick={() => { setTourLanded(true); setTourOn(true) }}>
            Restart guided tour
          </button>
        )}
        {signedOut && authConfigured && <SignedOutGate onDemo={() => setMock(true)} onSignIn={login} />}
        <div className="kicker">Home · one prompt, two lanes</div>
        <h1 className="home-q">What should Leaf do to <em>{projectName}</em>?</h1>
        <div className="hint">
          Try <b>count panels per layer</b> — one prompt, routed across <b>Run</b> ·{' '}
          <b>Build</b>. You confirm before anything runs — paid actions never auto-execute.
        </div>

        {!mock && openProjectId && (
          <WorkspaceSummary
            workspace={workspace}
            loading={wsLoading}
            selectedVersionId={canonicalVersionId}
            onSelectVersion={setCanonicalVersionId}
            onClose={onCloseProject}
          />
        )}

        <div className="workspace-card enter" style={{ '--rank': 1 }} ref={workspaceCardRef}>
          <div className="viewer-toolbar">
            <div className="viewer-title">
              {/* One loading voice per pane — the pulse-dot line in the viewer
                  announces loading; the title placeholder stays a muted dash. */}
              {shown ? `${projectName}.dwg` : <span className="dim">—</span>}
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
              {/* Version-completed events surface as NT2 toasts; only the genuine
                  read-only-preview advisory keeps an amber note here. */}
              {!mock && previewing && (
                <span className="version-note readonly">viewing v{previewing.version} · read-only</span>
              )}
              {!mock && (
                <CheckoutControls
                  lockedByOther={otherHeldCheckout}
                  heldByUs={heldByUs}
                  busy={checkoutBusy}
                  onTake={onTakeCheckout}
                  onRelease={onReleaseCheckout}
                />
              )}
              {drawingState && (
                <>
                  <button
                    className="btn ghost"
                    onClick={onUndo}
                    disabled={versionBusy || running || !!previewing || drawingState.head <= 1}
                  >
                    Undo
                  </button>
                  <button
                    className="btn ghost"
                    onClick={onRedo}
                    disabled={versionBusy || running || !!previewing || drawingState.head >= drawingState.latest}
                  >
                    Redo
                  </button>
                  <div className="vh-anchor">
                    <button
                      className="btn ghost"
                      onClick={onToggleHistory}
                      aria-expanded={historyOpen}
                      disabled={versionBusy}
                    >
                      History{previewing ? ` · v${previewing.version}` : ''}
                    </button>
                    {historyExit.shown && (
                      <VersionHistory
                        data={history}
                        error={historyErr}
                        loading={historyLoading}
                        previewingVersion={previewing?.version ?? null}
                        onPreview={onPreviewVersion}
                        onBackToHead={onBackToHead}
                        onClose={() => setHistoryOpen(false)}
                        onRetry={loadHistory}
                        retryKey={rTarget === 'history'}
                        exiting={historyExit.exiting}
                      />
                    )}
                  </div>
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
          {/* X1: a failed post-write viewer refresh — red row + Retry + honest
              fallback note (the completion itself already toasted plainly). */}
          {refreshFail && (
            <div className="inline-error" style={{ margin: '0 0 8px' }}>
              Couldn’t refresh the viewer — showing the previous version
              <button type="button" className="chip-act" onClick={onRetryViewerRefresh}>Retry</button>
              {rTarget === 'refresh' && <span className="key" aria-hidden="true">R</span>}
            </div>
          )}
          <div className="viewer-wrap">
            {/* X3 whole-pane takeover: red dot + what failed + quiet reason + Retry. */}
            {loadErr && !signedOut && (
              <div className="pane-fail" role="alert" style={{ position: 'absolute', inset: 0 }}>
                <span className="pane-fail-title"><span className="dot red" aria-hidden="true" />Couldn’t load drawing</span>
                <span className="pane-fail-reason">{loadErr}</span>
                <button className="chip-act" onClick={() => setIntakeRetryKey((k) => k + 1)}>Retry</button>
                {/* There is always a way home: in live mode a dead backend makes
                    Retry unwinnable, so offer the same demo escape the
                    SignedOutGate already gives. */}
                {!mock && (
                  <button className="chip-act" onClick={() => setMock(true)}>Back to the demo</button>
                )}
              </div>
            )}
            {signedOut && <div className="overlay-msg">Sign in or explore the demo to load a drawing.</div>}
            {!intake && !loadErr && !signedOut && (
              // Indeterminate load: content-shaped pulse dot + verb, top-left —
              // the centered takeover position is reserved for failures (X3).
              <div className="loading-line dim" style={{ position: 'absolute', top: 14, left: 14 }}>
                <span className="dot live pulse" aria-hidden="true" /> Loading drawing
              </div>
            )}
            {intake && (
              <Suspense fallback={<ViewerSkeleton />}>
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
              </Suspense>
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

        <div className="result-block enter" style={{ '--rank': 2 }} ref={resultBlockRef}>
          <ResultPanel
            running={running}
            runStatus={runStatus}
            runProgress={runProgress}
            runElapsedMs={runElapsedMs}
            error={runErr}
            result={result}
            tool={selectedTool}
            onRetry={onRetry}
            notices={
              /* NR banners dock UNDER the header of the affected pane (the
                 result) — rendered right after its <h3>; 2+ conditions collapse
                 to one line with a count instead of stacking. */
              advisories.length >= 2 ? (
                <div className="banner">
                  <span>{advisories.length} advisories — {advisories.join(' · ')}</span>
                </div>
              ) : (
                <>
                  {quotaShown && <QuotaCard message={quotaShown.message} remaining={usage?.cap?.remaining} onAction={openSessionDetails} />}
                  {runQuotaShown && (
                    <QuotaCard
                      kind="daily_runs"
                      message={runQuotaShown.error?.message}
                      tier={runQuotaShown.tier}
                      limit={runQuotaShown.limit}
                      used={runQuotaShown.used}
                      onAction={openSessionDetails}
                    />
                  )}
                  {entitlementError && (
                    <EntitlementNotice
                      required={entitlementError.required}
                      tier={entitlementError.tier || entTier}
                      message={entitlementError.error?.message}
                    />
                  )}
                  {degraded && <DegradedBanner />}
                </>
              )
            }
          />
          {/* Quiet Details -> the run's DT2 provenance drawer (receipt area). */}
          {result && !running && (
            <div className="result-details-row">
              <button type="button" className="chip-act" onClick={openRunDetails}>Details</button>
            </div>
          )}
        </div>

        {/* Agent tier (wire §11): the conversational surface for this drawing's
            session. LIVE only — never rendered in mock. 'race' keeps the chip
            primary; taking the chip unmounts this (onRun) without cancelling
            the server-side turn. */}
        {!mock && agentMode && agentSessionId && (
          <ConversePanel
            sessionId={agentSessionId}
            userTurns={agentTurns}
            onDismiss={() => setAgentMode(null)}
            onLinkClaude={() => setClaudeOpen(true)}
            onAttachJob={onAttachAgentJob}
            onJobLinked={refreshJobs}
          />
        )}

        <EntitlementGate
          tier={entTier}
          entitlements={entitlements}
          loading={entLoading}
          mock={mock}
        />
        </main>

        <div className="bar-dock">
          {/* SB3 state strips ride above the well: running / failed. Decisions
              (the route) attach as resolver rows / decision strips below. */}
          {running && (
            <div className="strip-running enter">
              <span className="dot live pulse" aria-hidden="true" />
              <span className="verb">
                {(runProgress || runStatus || 'running')}
                {selectedTool?.name ? ` — ${selectedTool.name}` : ''}
                {runElapsedMs != null ? ` · ${fmtElapsed(runElapsedMs)}` : ''}
              </span>
              <span className="key hot">Esc</span>
              <span className="dim">interrupt</span>
            </div>
          )}
          {!running && (runErr || routeErr) && (
            <div className="strip-failed enter">
              <span className="dot red" aria-hidden="true" />
              <span className="strip-sentence">
                {routeErr
                  ? `Couldn’t route the prompt — ${routeErr}`
                  : `Couldn’t run ${selectedTool?.name || 'the tool'} — ${runErr}`}
                <span className="dim"> · your last good result is unchanged</span>
              </span>
              <button
                type="button"
                className="chip-act"
                onClick={routeErr ? onDispatch : onRetry}
              >
                Retry
              </button>
              <span className="key">R</span>
            </div>
          )}
          {/* Calm agent-tier advisory (degraded fallback / quota / grant):
              amber square dot + sentence — the deterministic result above it
              rendered exactly as today; this only says why there's no chat. */}
          {agentBanner && !running && (
            <div className="strip-decision enter" role="status">
              <span className="dot square" aria-hidden="true" />
              <span className="strip-sentence">{agentBanner.message}</span>
              {agentBanner.kind === 'grant' && (
                <button type="button" className="chip-act" onClick={() => setClaudeOpen(true)}>
                  Link account
                </button>
              )}
              <button type="button" className="chip-neutral" onClick={() => setAgentBanner(null)}>
                Dismiss
              </button>
            </div>
          )}
          <RoutePanel
            route={route}
            tools={tools}
            running={running || !!previewing}
            writeLocked={writeLocked}
            writeEntitled={canRunWrite}
            onConfirmIntent={onConfirmCatalogRun}
            onPickAlternative={onPickAlternative}
            onOpenAuthor={onOpenAuthor}
            onDismiss={dismissRoute}
          />
          <PromptBox
            value={prompt}
            onChange={onPromptChange}
            onDispatch={onDispatch}
            routing={routing}
            hintLane={hintLane}
            projectName={currentProjectName || projectName}
            inputRef={barInputRef}
            routeActive={!!route}
            onOpenAuthor={onOpenAuthor}
            tools={slashTools}
          />
        </div>

        {/* The golden path's payoff (result numbers) and the running strip are
            both silent to a screen reader. One PERMANENTLY-mounted polite region
            announces the mutation; styles inline because no .sr-only utility
            exists in the sheet. */}
        <div
          role="status"
          aria-live="polite"
          style={{
            position: 'absolute', width: 1, height: 1, padding: 0, margin: -1,
            overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0,
          }}
        >
          {running
            ? `Running ${selectedTool?.name || 'tool'}`
            : result?.ok
              ? `${result.tool} complete${result.result?.total != null ? ` — total ${Number(result.result.total).toLocaleString()}` : ''}`
              : ''}
        </div>

        <Toast toast={toast} onDone={onToastDone} />
      </div>

      <JobRail
        mock={mock}
        jobs={jobs}
        currentJob={currentJob}
        inflight={inflightPtr}
        reattaching={reattaching}
        onSelectJob={onSelectJob}
      />

      <footer className="foot-bar">
        {/* Traversal left: a named "← Parent" link while a project is open. */}
        {!mock && openProjectId && (
          <button type="button" className="chip-act" onClick={onCloseProject}>← All projects</button>
        )}
        {/* Real statuses get the 6px dot + tinted sentence-case word; counts and
            spend are muted metadata (green is reserved for genuine states). */}
        {mock ? (
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">mock (no cloud)</span></span>
        ) : authRequired ? (
          /* the ONLY unauthenticated signal is the public /api/health ping — a
             rosy "cloud live · N tools" over a 401-walled app would be a lie */
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">sign-in required</span></span>
        ) : health ? (
          health.aps_live
            ? <span className="foot-stat"><span className="dot" aria-hidden="true" />backend · <span className="ok-txt">cloud live</span></span>
            : <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">local only</span></span>
        ) : !authConfigured ? (
          /* Gating window (a VITE_MOCK=0 build with Auth0 unconfigured, before the
             401 auto-fallback flips to mock): never claim a green "live" state we
             haven't confirmed. Neutral until the fallback lands. */
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">connecting…</span></span>
        ) : (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />backend · <span className="ok-txt">live</span></span>
        )}
        {mock ? (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />local solver · <span className="ok-txt">ready</span></span>
        ) : authRequired ? null : health ? (
          <span className="foot-stat">
            <span className={health.da_client_present ? 'dot' : 'dot square'} aria-hidden="true" />
            data agent · <span className={health.da_client_present ? 'ok-txt' : 'warn-txt'}>
              {health.da_client_present ? 'ready' : 'absent'}
            </span> · <span className="dim">{plural(health.n_tools, 'tool')}</span>
          </span>
        ) : (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />local solver · <span className="ok-txt">ready</span></span>
        )}
        <span className="dim">{plural(capCount, 'cap')} · {catalog.families.length} famil{catalog.families.length === 1 ? 'y' : 'ies'} · tier {gateTier}</span>
        {!mock && usage && (
          <span className="dim">${Number(usage.today?.usd_est || 0).toFixed(3)} today</span>
        )}
        <span style={{ marginLeft: 'auto' }}>build <span style={{ fontFamily: 'var(--font-mono)' }}>{__BUILD_HASH__}</span> · {mock ? 'sample data' : 'live'}</span>
      </footer>

      {opsExit.shown && <OpsDrawer onDismiss={() => setOpsDismissed(true)} exiting={opsExit.exiting} />}

      {/* DT2 drawer: fixed over the events rail (row 2, col 3) — the rail
          behind never re-flows. Esc (global ladder) or the header cap closes. */}
      <DetailsDrawer data={drawer} onClose={() => setDrawer(null)} />

      {/* M5: the ?demo=tour walkthrough. Mock-only — the tour drives real mock
          handlers, so it must never point at a live/paid backend. */}
      {mock && tourOn && (
        <DemoTour
          onCannedPrompt={onCannedPrompt}
          onExit={onTourExit}
          landed={tourLanded && !running && !routing}
          busy={running || routing}
        />
      )}
    </div>
  )
}
