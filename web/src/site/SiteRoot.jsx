// Website One Surface — the scene machine over the persistent stage.
// Scenes: site (cover) · tool (recast) · sheets (lazy sibling) · app (the
// untouched console, rendered ALONE — App owns its own Viewer).
//
// BOOT BACK-COMPAT (critical): any of ?demo= ?fixture= ?ops= ?dev= ?drawing=
// or an Auth0 callback (?code= AND ?state=) boots straight into scene 'app'
// regardless of path — every pre-existing deep link keeps working byte-for-
// byte. Checked ONCE at boot; navigate() preserves the search string.

import React, { Suspense, useEffect, useRef, useState } from 'react'
import { useRoute, navigate } from './router.js'
import StageLayer from './StageLayer.jsx'
import LandingCast from './LandingCast.jsx'
import ToolCast from './ToolCast.jsx'
import { WorkspaceControllerProvider } from '../controllers/WorkspaceControllerProvider.jsx'
import './landing.css'

const App = React.lazy(() => import('../App.jsx'))
// Built by a sibling agent (src/site/sheets/** is theirs) — referenced only.
const SheetsPage = React.lazy(() => import('./sheets/SheetsPage.jsx'))

const APP_BOOT_PARAMS = ['demo', 'fixture', 'ops', 'dev', 'drawing']

function bootWantsApp(search) {
  try {
    const q = new URLSearchParams(search)
    if (APP_BOOT_PARAMS.some((k) => q.has(k))) return true
    if (q.has('code') && q.has('state')) return true
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

export default function SiteRoot() {
  // Evaluated once at boot — deep links into the console never see the site.
  const [bootApp] = useState(() => bootWantsApp(window.location.search))
  const { path } = useRoute()
  const scene = bootApp ? 'app' : sceneForPath(path)
  const stageRef = useRef(null)
  const [operatorIntake, setOperatorIntake] = useState(null)

  // Keyboard recasts — ONLY in scenes site|tool (listener not registered in
  // scene app/sheets), and never when focus is in an editable element.
  useEffect(() => {
    if (scene !== 'site' && scene !== 'tool') return undefined
    const onKey = (e) => {
      if (isEditable(e.target)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'Escape' && scene === 'tool') {
        const ownedSurface = document.querySelector('.proj-menu, .route, .drawer-layer .drawer, .claude-pop')
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

  if (scene === 'sheets') {
    return (
      <Suspense fallback={null}>
        <SheetsPage />
      </Suspense>
    )
  }

  return (
    <WorkspaceControllerProvider
      key={scene}
      drawingId={scene === 'tool' ? 'cat-panels' : 'rooftop_demo'}
    >
      <div className="stage-root" data-scene={scene} ref={stageRef}>
        <StageLayer intakeOverride={scene === 'tool' ? operatorIntake : null} />
        <LandingCast onTryTool={() => navigate('/try')} />
        <ToolCast active={scene === 'tool'} onIntakeChange={setOperatorIntake} />
      </div>
    </WorkspaceControllerProvider>
  )
}
