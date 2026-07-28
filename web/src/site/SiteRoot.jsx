// Website One Surface — the scene machine over the persistent stage.
// Scenes: site (cover) · tool (recast) · sheets (lazy sibling) · app (the
// untouched console, rendered ALONE — App owns its own Viewer).
//
// BOOT BACK-COMPAT (critical): any of ?demo= ?fixture= ?ops= ?dev= ?drawing=
// or an Auth0 callback (?code= AND ?state=) boots straight into scene 'app'
// regardless of path — every pre-existing deep link keeps working byte-for-
// byte. Checked ONCE at boot; navigate() preserves the search string.

import React, { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { useRoute, navigate } from './router.js'
import StageLayer from './StageLayer.jsx'
import LandingCast from './LandingCast.jsx'
import ToolCast from './ToolCast.jsx'
import { WorkspaceControllerProvider } from '../controllers/WorkspaceControllerProvider.jsx'
import { handleRedirectCallback, isSignedIn } from '../auth.js'
import {
  getDrawingIntake,
  getDrawingVersions,
  redoDrawing,
  undoDrawing,
} from '../api.js'
import './landing.css'

const App = React.lazy(() => import('../App.jsx'))
// Built by a sibling agent (src/site/sheets/** is theirs) — referenced only.
const SheetsPage = React.lazy(() => import('./sheets/SheetsPage.jsx'))

const APP_BOOT_PARAMS = ['fixture', 'dev', 'drawing']

function bootWantsApp(search, path = window.location.pathname) {
  try {
    const q = new URLSearchParams(search)
    if (APP_BOOT_PARAMS.some((k) => q.has(k))) return true
    if (q.has('demo') && path !== '/try') return true
    if (q.has('ops') && path !== '/try') return true
    if (q.has('code') && q.has('state') && path !== '/try') return true
  } catch { /* malformed search — fall through to path routing */ }
  return false
}

function sceneForPath(path) {
  if (path === '/app' || path.startsWith('/app/')) return 'app'
  if (path === '/sheets' || path.startsWith('/sheets/')) return 'sheets'
  if (path === '/try') return 'tool'
  return 'site'
}

const isEditable = (el) =>
  !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)

const OPERATOR_DRAWING_ID = 'cat-panels'
const DEMO_REQUESTED = new URLSearchParams(window.location.search).get('demo') === '1'
const PUBLIC_DEMO = DEMO_REQUESTED && !isSignedIn()
const loadHead = (drawingId) => getDrawingIntake(PUBLIC_DEMO, drawingId, 'head')
const loadVersion = (drawingId, version) => getDrawingIntake(PUBLIC_DEMO, drawingId, version)
const loadVersions = (drawingId) => getDrawingVersions(PUBLIC_DEMO, drawingId)
const undoVersion = (drawingId, capability) => undoDrawing(PUBLIC_DEMO, drawingId, capability)
const redoVersion = (drawingId, capability) => redoDrawing(PUBLIC_DEMO, drawingId, capability)

export default function SiteRoot() {
  // Evaluated once at boot — deep links into the console never see the site.
  const [bootApp] = useState(() => bootWantsApp(window.location.search, window.location.pathname))
  const [authCallbackPending, setAuthCallbackPending] = useState(() => {
    const query = new URLSearchParams(window.location.search)
    return window.location.pathname === '/try' && query.has('code') && query.has('state')
  })
  const { path } = useRoute()
  const scene = bootApp ? 'app' : sceneForPath(path)
  const stageRef = useRef(null)
  const stageLayerRef = useRef(null)
  const [operatorIntake, setOperatorIntake] = useState(null)
  const [operatorViewMode, setOperatorViewMode] = useState('flat')
  const [operatorVisibleLayers, setOperatorVisibleLayers] = useState(null)
  const [operatorSelectedHandle, setOperatorSelectedHandle] = useState(null)
  const [operatorOverlay, setOperatorOverlay] = useState(null)
  const drawingOptions = useMemo(() => ({
    loadHead,
    loadVersion,
    loadVersions,
    undoVersion,
    redoVersion,
    onApplyIntake: setOperatorIntake,
    onResetSelection: () => setOperatorSelectedHandle(null),
    initialDrawingState: {
      drawing_id: OPERATOR_DRAWING_ID,
      version: 1,
      head: 1,
      latest: 1,
    },
  }), [])

  useEffect(() => {
    if (!authCallbackPending) return
    let live = true
    handleRedirectCallback().then((signedIn) => {
      if (!live) return
      if (signedIn) window.location.reload()
      else setAuthCallbackPending(false)
    })
    return () => { live = false }
  }, [authCallbackPending])

  // Keyboard recasts — ONLY in scenes site|tool (listener not registered in
  // scene app/sheets), and never when focus is in an editable element.
  useEffect(() => {
    if (scene !== 'site' && scene !== 'tool') return undefined
    const onKey = (e) => {
      if (isEditable(e.target)) return
      if (scene === 'tool' && (e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        stageRef.current?.querySelector('.tc-bar-input')?.focus()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'Escape' && scene === 'tool') {
        const ownedSurface = document.querySelector('.proj-menu, .route, .strip-decision, .resolver, .drawer-layer .drawer, .claude-pop')
        if (!ownedSurface) navigate('/')
      }
      else if ((e.key === 't' || e.key === 'T') && scene === 'site') navigate('/try')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [scene])

  // The inactive cast is inert + aria-hidden (pointer-events already die in
  // CSS; this removes it from tab order and the accessibility tree too).
  useEffect(() => {
    const root = stageRef.current
    if (!root) return
    const activeCast = scene === 'tool' ? 'tool' : 'site'
    root.querySelectorAll('[data-cast]').forEach((el) => {
      const cast = el.getAttribute('data-cast')
      const hidden = cast !== 'both' && cast !== activeCast
      el.toggleAttribute('inert', hidden)
      if (hidden) el.setAttribute('aria-hidden', 'true')
      else el.removeAttribute('aria-hidden')
    })
  }, [scene])

  if (scene === 'app') {
    // The console ALONE — no stage mounted; App owns its own Viewer.
    return (
      <WorkspaceControllerProvider drawingId="rooftop_demo" retryNotFound>
        <Suspense fallback={null}>
          <App />
        </Suspense>
      </WorkspaceControllerProvider>
    )
  }

  if (authCallbackPending) return <div className="site-auth-callback" role="status">Completing sign in</div>

  if (scene === 'sheets') {
    return (
      <Suspense fallback={null}>
        <SheetsPage />
      </Suspense>
    )
  }

  return (
    <WorkspaceControllerProvider
      drawingId={scene === 'tool' ? 'cat-panels' : 'rooftop_demo'}
      drawingOptions={drawingOptions}
    >
      <main className="stage-root" data-scene={scene} ref={stageRef} aria-label={scene === 'tool' ? 'Leaf operator workspace' : 'Leaf product overview'}>
        <StageLayer
          ref={stageLayerRef}
          intakeOverride={scene === 'tool' ? operatorIntake : null}
          viewMode={scene === 'tool' ? operatorViewMode : 'flat'}
          visibleLayers={scene === 'tool' ? operatorVisibleLayers : null}
          selectedHandle={scene === 'tool' ? operatorSelectedHandle : null}
          onSelectEntity={scene === 'tool' ? setOperatorSelectedHandle : undefined}
          overlay={scene === 'tool' ? operatorOverlay : null}
        />
        <LandingCast onTryTool={() => navigate('/try')} />
        <ToolCast
          active={scene === 'tool'}
          onFitDrawing={() => stageLayerRef.current?.fit()}
          onViewModeChange={setOperatorViewMode}
          onVisibleLayersChange={setOperatorVisibleLayers}
          selectedHandle={operatorSelectedHandle}
          onSelectedHandleChange={setOperatorSelectedHandle}
          onResultOverlayChange={setOperatorOverlay}
        />
      </main>
    </WorkspaceControllerProvider>
  )
}
