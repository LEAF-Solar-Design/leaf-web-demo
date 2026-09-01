// Website One Surface — the scene machine over the persistent stage.
// Scenes: site (cover) · tool (recast) · sheets (lazy sibling) · app (the
// untouched console, rendered ALONE — App owns its own Viewer).
//
// BOOT BACK-COMPAT (critical): any of ?demo= ?fixture= ?ops= ?dev= ?drawing=
// or an Auth0 callback (?code= AND ?state=) boots straight into scene 'app'
// regardless of path — every pre-existing deep link keeps working byte-for-
// byte. Checked ONCE at boot; navigate() preserves the search string.
//
// W1 (convergence): DrawingIdentityProvider is mounted ONCE, ABOVE the scene
// branch, in the MODE the current scene implies — console for the /app
// console, operator for the stage — and the stage's casts moved into
// StageScene.jsx so they can READ that provider. Routing, the auth-callback
// deferral, the marketing redirect and the keyboard/inert passes are
// untouched.
//
// WHY ABOVE THE BRANCH (panel W1 finding 1): mounted inside the branches, the
// provider was a different element in the sheets arm, so a /try -> / ->
// /sheets -> /try round trip unmounted it and destroyed an in-progress
// (guest) upload identity — state SiteRoot's own `operatorDrawingId` used to
// carry through that detour. One provider at the root of every arm survives
// it; the provider keeps one identity PER MODE so the hoist cannot serve the
// console's identity to the stage or the reverse.

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

// THE reading of the boot query, made once (ACCEPTANCE route matrix: "one
// reading must serve both consumers"). The same string drives the BOOT
// decision (bootWantsApp) and the drawing SELECTION (classifyDemo, handed to
// the provider rather than re-read there), so the two cannot drift.
// navigate() preserves the search, and nothing else rewrites it.
const BOOT_SEARCH = window.location.search
const DEMO = classifyDemo(BOOT_SEARCH, isSignedIn())
const PUBLIC_DEMO = DEMO.publicDemo

export default function SiteRoot() {
  // Evaluated once at boot — deep links into the console never see the site.
  const [bootApp] = useState(() => bootWantsApp(BOOT_SEARCH, window.location.pathname))
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

  // will-change hygiene: the [data-cast] panes only need a GPU layer during
  // the actual recast (landing.css .stage-root.recasting), never at rest.
  // The window covers exit(870ms) + gap(900ms) + the longer enter transition,
  // the 2250ms filter/focus pull, with slack: 870+900+2250 = 4020ms.
  // Skipped entirely under reduced motion (transitions already collapse to
  // 0s there — see landing.css:733 — so there is nothing to hint about).
  const prevSceneRef = useRef(scene)
  useEffect(() => {
    const root = stageRef.current
    if (!root) return undefined
    if (prevSceneRef.current === scene) return undefined
    prevSceneRef.current = scene
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return undefined
    const RECAST_WINDOW_MS = 4100 // exit(870) + gap(900) + enter-filter(2250) + slack
    root.classList.add('recasting')
    const timer = setTimeout(() => root.classList.remove('recasting'), RECAST_WINDOW_MS)
    return () => {
      clearTimeout(timer)
      root.classList.remove('recasting')
    }
  }, [scene])

  // BEFORE every scene, including 'app'. Mounting anything that talks to /api
  // while the code exchange is still in flight is the whole defect: the burst
  // 401s, the session controller latches `required`, and the token that lands
  // 700ms later can no longer be used by this page load.
  if (authCallbackPending) return <div className="site-auth-callback" role="status">Completing sign in</div>

  // ONE provider, at the root of every arm, so React reconciles it across
  // scene changes instead of unmounting it. `mode` follows the scene; the
  // provider keeps an identity per mode, so the console and the stage never
  // read each other's.
  return (
    <DrawingIdentityProvider
      mode={scene === 'app' ? DRAWING_MODE_CONSOLE : DRAWING_MODE_OPERATOR}
      search={BOOT_SEARCH}
      publicDemo={DEMO.publicDemo}
      liveDemo={DEMO.liveDemo}
    >
      {scene === 'app' ? (
        // The console ALONE — no stage mounted; App owns its own Viewer.
        <WorkspaceControllerProvider drawingId="rooftop_demo" retryNotFound>
          <Suspense fallback={null}>
            <App />
          </Suspense>
        </WorkspaceControllerProvider>
      ) : scene === 'sheets' ? (
        <Suspense fallback={null}>
          <SheetsPage />
        </Suspense>
      ) : (
        <StageScene scene={scene} stageRef={stageRef} publicDemo={PUBLIC_DEMO} />
      )}
    </DrawingIdentityProvider>
  )
}
