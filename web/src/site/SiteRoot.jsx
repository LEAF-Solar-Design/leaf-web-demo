// Website One Surface — the scene machine over the persistent stage.
// Scenes: site (cover) · tool (recast) · sheets (lazy sibling) · app (the
// untouched console, rendered ALONE — App owns its own Viewer).
//
// BOOT BACK-COMPAT (critical): any of ?demo= ?fixture= ?ops= ?dev= ?drawing=
// or an Auth0 callback (?code= AND ?state=) boots straight into scene 'app'
// regardless of path — every pre-existing deep link keeps working byte-for-
// byte. Checked ONCE at boot; navigate() preserves the search string.
//
// W1 (convergence): each scene now mounts DrawingIdentityProvider in its MODE
// — console for the /app console, operator for the stage — and the stage's
// casts moved into StageScene.jsx so they can READ that provider. Routing,
// the auth-callback deferral, the marketing redirect and the keyboard/inert
// passes are untouched.

import React, { Suspense, useEffect, useRef, useState } from 'react'
import { markInstant } from '../lib/instant.js'
import { useRoute, navigate } from './router.js'
import { sceneForPath } from './routeScene.js'
import StageScene from './StageScene.jsx'
import { WorkspaceControllerProvider } from '../controllers/WorkspaceControllerProvider.jsx'
import { handleRedirectCallback, isSignedIn } from '../auth.js'
import { bootWantsApp, shouldDeferForAuthCallback } from './authBoot.js'
import {
  DRAWING_MODE_CONSOLE,
  DRAWING_MODE_OPERATOR,
  DrawingIdentityProvider,
} from '../drawing/DrawingIdentityProvider.jsx'
import { classifyDemo } from '../drawing/drawingIdentity.js'
import './landing.css'

const App = React.lazy(() => import('../App.jsx'))
// Built by a sibling agent (src/site/sheets/** is theirs) — referenced only.
const SheetsPage = React.lazy(() => import('./sheets/SheetsPage.jsx'))

const isEditable = (el) =>
  !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)

// Front-door contract (contract/FRONT-DOOR.md; leaf_website decision D2,
// docs/decisions/public-web-overhaul-20260819.md): these deployed hosts are
// APP-ONLY — the one indexable marketing front door is www.leafautomation.ai.
// Marketing scenes (site/sheets) redirect there. Auth0's bare-origin callback
// and every ?demo/?fixture deep link boot scene 'app' first (bootWantsApp),
// so they never reach the redirect. localhost/dev and Vercel preview builds
// are deliberately NOT listed: the site scenes stay viewable for development
// and review there (index.html carries noindex everywhere regardless).
const APP_ONLY_HOSTS = new Set([
  'leaf-platform-web.vercel.app',
  'platform.leafdesign.ai',
  'platform-staging.leafdesign.ai',
])
const MARKETING_ORIGIN = 'https://www.leafautomation.ai'

// The ONE `?demo` reading on this surface (ACCEPTANCE route matrix: "one
// reading must serve both consumers"). bootWantsApp takes the same search
// string for the BOOT decision; this classification is what the drawing
// SELECTION is seeded from, handed to the provider rather than re-read there.
const DEMO = classifyDemo(window.location.search, isSignedIn())
const PUBLIC_DEMO = DEMO.publicDemo

export default function SiteRoot() {
  // Evaluated once at boot — deep links into the console never see the site.
  const [bootApp] = useState(() => bootWantsApp(window.location.search, window.location.pathname))
  // Armed by the callback QUERY on ANY path, not just /try: the redirect_uri is
  // the bare origin, so /try is the one landing this never sees. See authBoot.js.
  const [authCallbackPending, setAuthCallbackPending] = useState(shouldDeferForAuthCallback)
  const { path } = useRoute()
  const scene = bootApp ? 'app' : sceneForPath(path)
  // Marketing scenes leave for the real front door on app-only hosts. The
  // sheets codes differ between the two surfaces ('02'.. here, 'l-000'.. on
  // www), so any /sheets path lands on the www sheets hub, not a 404.
  useEffect(() => {
    if (scene !== 'site' && scene !== 'sheets') return
    if (!APP_ONLY_HOSTS.has(window.location.hostname)) return
    if (shouldDeferForAuthCallback()) return
    const target = scene === 'sheets' ? '/sheets' : window.location.pathname
    window.location.replace(MARKETING_ORIGIN + target)
  }, [scene])
  const stageRef = useRef(null)

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
        // data-instant (W0#7): the focus hotkey lands frame-of-keypress; the
        // scene recasts (Esc/T -> navigate) stay Register III on purpose.
        markInstant()
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

  // BEFORE every scene, including 'app'. Mounting anything that talks to /api
  // while the code exchange is still in flight is the whole defect: the burst
  // 401s, the session controller latches `required`, and the token that lands
  // 700ms later can no longer be used by this page load.
  if (authCallbackPending) return <div className="site-auth-callback" role="status">Completing sign in</div>

  if (scene === 'app') {
    // The console ALONE — no stage mounted; App owns its own Viewer.
    return (
      <DrawingIdentityProvider mode={DRAWING_MODE_CONSOLE}>
        <WorkspaceControllerProvider drawingId="rooftop_demo" retryNotFound>
          <Suspense fallback={null}>
            <App />
          </Suspense>
        </WorkspaceControllerProvider>
      </DrawingIdentityProvider>
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
    <DrawingIdentityProvider
      mode={DRAWING_MODE_OPERATOR}
      publicDemo={DEMO.publicDemo}
      liveDemo={DEMO.liveDemo}
    >
      <StageScene scene={scene} stageRef={stageRef} publicDemo={PUBLIC_DEMO} />
    </DrawingIdentityProvider>
  )
}
