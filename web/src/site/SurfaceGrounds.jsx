// The per-surface GROUND under the studio shell (W4a, docs/convergence/
// ACCEPTANCE.md "Surface grounds"). The W3 mount put the console's drawing on
// the z0 ground for every tab, which only reads as a workspace on CAD and
// Solar CAD. Here the ground becomes "the actual workspace" for each surface
// (operator direction 2026-09-01/02): the drawing for CAD and Solar CAD, an
// open-project BOARD for Browser, a device STAGE for the iOS ship lane.
//
// Contract, same as the viewer ground:
//   * Rendered ONLY through App's studio-ground portal (rail ON). The old
//     shell has no ground, so none of this exists there — the rollback path
//     is untouched by construction, and siteRootOneShell.test.js pins it.
//   * Every ground is props-driven and fails HONEST: real state renders,
//     absent state says so ("No jobs yet"), nothing is invented — the same
//     rule productSurfaces/workspaceProjectState/IosSurface already obey.
//   * Exactly one ground is visible at a time (the `hidden` attribute, never
//     an unmount): the drawing ground survives tab switches with its WebGL
//     context, lock, and job state, exactly as the workspace card always
//     did (`display: none`, not unmount).
import { Children, useLayoutEffect, useRef, useState } from 'react'
import WorldSpaceBoard from './WorldSpaceBoard.jsx'
import { computeSafeRect } from './useDrawingViewport.js'
import { START_BOARD_COPY } from './startBoardCopy.js'
import { isWriteTool } from '../lib/toolRecord.js'
import { formatElementId } from '../lib/elementIdentity.js'
import { PRODUCT_SURFACES, SHARED_WORKSPACE_CAPABILITIES, surfaceGround } from './productSurfaces.js'
import { EMPTY_WORKSPACE_PROJECT } from './workspaceProjectState.js'
import { deriveIosState, humanizeStage, IOS_STATE_LABEL } from '../ios/IosSurface.jsx'

// THE WINDOW. On Browser and iOS the product frame (#product-surface-panel)
// is transparent chrome over the ground, like the workspace card over the
// drawing; its head, title, and project line stay at the top and the rest
// of the frame is the window the ground shows through. The ground lays its
// content INTO that window by measuring it — never by guessing the
// console's grid: the project line's height changes with state (a
// drawing-only state adds an explainer, an action, and a reason), the rails
// change width at 1200px, and the center column scrolls. ResizeObserver +
// scroll + resize keep it exact; jsdom and any measurement that yields no
// height fall back to the CSS-variable geometry in landing.css.
const WINDOW_GUTTER = 14
const MIN_WINDOW_HEIGHT = 160

export function measureGroundWindow(doc = document) {
  const panel = doc.getElementById('product-surface-panel')
  if (!panel) return null
  const frame = panel.getBoundingClientRect()
  if (!(frame.height > 0) || !(frame.width > 0)) return null
  // The chrome is everything above the window: the last of the project
  // line, the description, the title, or the head, whichever renders last.
  const chrome = panel.querySelector('.tc-product-project')
    || panel.querySelector('.tc-product-frame h1 + p, .tc-product-morph > p')
    || panel.querySelector('h1')
    || panel.querySelector('.tc-product-frame-head')
  const chromeBottom = chrome ? chrome.getBoundingClientRect().bottom : frame.top
  const top = chromeBottom + WINDOW_GUTTER
  const height = frame.bottom - WINDOW_GUTTER - top
  if (!(height >= MIN_WINDOW_HEIGHT)) return null
  const inset = 21 // the frame's own horizontal padding
  return {
    top: Math.round(top),
    left: Math.round(frame.left + inset),
    width: Math.round(frame.width - inset * 2),
    height: Math.round(height),
  }
}

export function measureContainedWindow(board, doc = document, occluders = []) {
  const origin = board?.getBoundingClientRect()
  if (!(origin?.width > 0) || !(origin?.height > 0)) return null
  return computeSafeRect(origin, occluders.map(([selector, edge, options]) => ({
    rect: doc.querySelector(selector)?.getBoundingClientRect(), edge, reserve: options?.reserve,
  })), { padding: 16 })
}

const NO_OCCLUDERS = []

function useGroundWindow(active, contained = false, boardRef, occluders = NO_OCCLUDERS) {
  const [rect, setRect] = useState(null)
  useLayoutEffect(() => {
    if (!active || typeof window === 'undefined') { setRect(null); return undefined }
    let frame = 0
    let observed = new Set()
    let observer = null
    const measure = () => {
      frame = 0
      const next = contained ? measureContainedWindow(boardRef.current, document, occluders) : measureGroundWindow()
      if (contained && observer) {
        const targets = new Set([boardRef.current, ...occluders.map(([selector]) => document.querySelector(selector))].filter(Boolean))
        for (const element of observed) if (!targets.has(element)) observer.unobserve(element)
        for (const element of targets) if (!observed.has(element)) observer.observe(element)
        observed = targets
      }
      setRect((prev) => {
        if (!next) return null
        if (prev && prev.top === next.top && prev.left === next.left
          && prev.width === next.width && prev.height === next.height) return prev
        return next
      })
    }
    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(measure) }
    const panel = contained ? boardRef.current : document.getElementById('product-surface-panel')
    const scroller = document.querySelector('main.center-scroll')
    observer = (typeof ResizeObserver !== 'undefined' && panel) ? new ResizeObserver(schedule) : null
    if (!contained) {
      observer?.observe(panel)
      if (observer && scroller) observer.observe(scroller)
    }
    measure()
    const discovery = contained && typeof MutationObserver !== 'undefined' ? new MutationObserver((records) => {
      if (records.some((record) => [...record.addedNodes, ...record.removedNodes].some((node) =>
        node.nodeType === 1 && occluders.some(([selector]) => node.matches(selector) || node.querySelector(selector))))) schedule()
    }) : null
    discovery?.observe(document.body, { childList: true, subtree: true })
    window.addEventListener('resize', schedule)
    scroller?.addEventListener('scroll', schedule, { passive: true })
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      observer?.disconnect()
      discovery?.disconnect()
      window.removeEventListener('resize', schedule)
      scroller?.removeEventListener('scroll', schedule)
    }
  }, [active, contained, boardRef, occluders])
  return rect
}

const windowStyle = (rect) => (rect
  ? { top: rect.top, left: rect.left, width: rect.width, height: rect.height, right: 'auto' }
  : undefined)

// Standardization slice 2: DERIVED from the Surface Contract instead of a
// hand-kept literal Set, so a surface's ground is declared in exactly one
// place (productSurfaces.js) and this file cannot drift from it. Computed once
// at module load, never per call: `has` stays O(1) on the hot render path.
// The truth table is unchanged from the old `new Set(['cad','solar'])`, and
// surfaceGates.test.js pins it. That includes the unknown/undefined case, which
// still answers false, because a Set lookup misses rather than normalizing.
// `contract?.` so a record that ever ships without a contract reads as "no
// drawing ground" instead of throwing during module load (a white screen
// before any error boundary exists). productSurfaces.test.js pins contract
// presence for every id, so today this guard never fires.
const DRAWING_SURFACES = new Set(
  PRODUCT_SURFACES.filter(({ contract }) => contract?.ground === 'drawing').map(({ id }) => id),
)

// The drawing ground shows for the surfaces whose declared ground is 'drawing'.
export function groundShowsDrawing(surface) {
  return DRAWING_SURFACES.has(surface)
}

const listCount = (list) => (Array.isArray(list) ? list.length : 0)

function capabilityTotal(families) {
  return families.reduce((count, family) => count + listCount(family.capabilities), 0)
}

function shortId(value, n = 8) {
  const text = String(value ?? '').trim()
  return text ? text.slice(0, n) : ''
}

// ---------------------------------------------------------------------------
// Browser: the open-project board. Tiles are the project's real objects —
// the mounted drawing, its versions, jobs, built tools, the live tenant
// catalog — plus the capabilities every surface shares. `workspace` is the
// same GET /api/projects/:id/workspace payload WorkspaceSummary renders;
// null (no project open, or the offline demo) renders the honest empties.
// ---------------------------------------------------------------------------
function BoardTiles({ workspace, drawing, catalog, renderTile, studioPresentation = false }) {
  const versions = workspace?.drawing_versions || []
  const jobs = [...(workspace?.jobs || [])].reverse().slice(0, 5) // newest first
  const tools = workspace?.built_tools || []
  const families = catalog?.families || []
  const tiles = (
    <>
          <section className="ground-tile" data-tile="drawing" aria-label="Drawing">
            <h3>Drawing</h3>
            {drawing ? (
              <>
                <strong>{drawing.name}</strong>
                <p>{drawing.polylines} polylines · {drawing.layers} layers</p>
              </>
            ) : <p className="ground-empty">No drawing mounted</p>}
          </section>
          <section className="ground-tile" data-tile="versions" aria-label="Versions">
            <h3>Versions</h3>
            {versions.length ? (
              <>
                <strong>{versions.length} drawing version{versions.length === 1 ? '' : 's'}</strong>
                <ul>
                  {[...versions].slice(-3).reverse().map((version) => (
                    <li key={version.version_id} data-element-id={formatElementId('version', version.version_id) || undefined}>v{version.seq} · {shortId(version.drawing_id)}</li>
                  ))}
                </ul>
              </>
            ) : <p className="ground-empty">{workspace ? 'No versions yet' : 'Versions live with a workspace project'}</p>}
          </section>
          <section className="ground-tile" data-tile="jobs" aria-label="Jobs">
            <h3>Jobs</h3>
            {jobs.length ? (
              <ul>
                {jobs.map((job) => (
                  <li key={job.job_id} data-element-id={formatElementId('job', job.job_id) || undefined}>
                    <strong>{job.tool_name || job.kind}</strong> · {job.status || 'pending'}
                  </li>
                ))}
              </ul>
            ) : <p className="ground-empty">{workspace ? 'No jobs yet' : 'Runs appear here with a project open'}</p>}
          </section>
          <section className="ground-tile" data-tile="tools" aria-label="Built tools">
            <h3>Built tools</h3>
            {tools.length ? (
              <ul>{tools.map((tool, i) => {
                const realId = tool.tool_id || tool.name || ''
                return (
                  <li key={realId || i} data-element-id={(realId && formatElementId('tool', realId)) || undefined}>
                    {tool.name || tool.tool_id}
                  </li>
                )
              })}</ul>
            ) : <p className="ground-empty">{workspace ? 'No built tools yet' : 'Authored tools attach to the project'}</p>}
          </section>
          <section className="ground-tile" data-tile="catalog" aria-label="Catalog">
            <h3>Catalog</h3>
            {families.length ? (
              <>
                <strong>{families.length} {families.length === 1 ? 'family' : 'families'} · {capabilityTotal(families)} tools</strong>
                <ul>{families.map((family) => (
                  <li key={family.family_id} data-element-id={formatElementId('family', family.family_id) || undefined}>{family.label}
                    {studioPresentation && <ul className="ground-catalog-tools">
                      {(family.capabilities || []).map((tool) => (
                        <li key={tool.name} className="ground-catalog-tool">
                          <strong>{tool.label || tool.name}</strong>
                          <p>{tool.description || START_BOARD_COPY.missingDescription}</p>
                          <p>{isWriteTool(tool) ? START_BOARD_COPY.changesDrawing
                            : Array.isArray(tool.capabilities) && tool.capabilities.includes('drawing.read')
                              ? START_BOARD_COPY.readsDrawing : START_BOARD_COPY.unspecifiedDrawingEffect}</p>
                        </li>
                      ))}
                    </ul>}
                  </li>
                ))}</ul>
              </>
            ) : <p className="ground-empty">Loading the live catalog</p>}
          </section>
          <section className="ground-tile" data-tile="shared" aria-label="Shared everywhere">
            <h3>Shared everywhere</h3>
            <ul>{SHARED_WORKSPACE_CAPABILITIES.map((capability) => <li key={capability}>{capability}</li>)}</ul>
          </section>
    </>
  )
  return renderTile ? Children.toArray(tiles.props.children).map((tile) => renderTile(tile.props['data-tile'], tile)) : tiles
}

export function ProjectBoardGround({
  active = false, workspaceProject = null, workspace = null, drawing = null, catalog = null, mock = false,
  leavingGround = null,
  contained = false, onReturnToDrawing = null, headingRef = null, startFocusRequest = 0,
  occluders = NO_OCCLUDERS,
  studioPresentation = false,
  worldSpace = import.meta.env.VITE_WORLD_SPACE_BOARD === '1', store,
}) {
  const state = workspaceProject || EMPTY_WORKSPACE_PROJECT
  const boardRef = useRef(null)
  const leaving = leavingGround === 'board'
  const win = useGroundWindow(active || leaving, contained, boardRef, occluders)
  const localHeadingRef = useRef(null)
  const containedHeadingRef = headingRef || localHeadingRef
  const lastFocusRequest = useRef(0)
  useLayoutEffect(() => {
    if (!active || !contained || !win || startFocusRequest <= lastFocusRequest.current) return
    lastFocusRequest.current = startFocusRequest
    containedHeadingRef.current?.focus()
  }, [active, contained, win, startFocusRequest, containedHeadingRef])
  // Browser uses the frame's chrome. Drafting Start owns its header inside
  // the ground because a drawing profile has no product frame.
  return (
    <div
      className="studio-ground-board"
      ref={boardRef}
      data-ground="browser"
      data-board-layout={contained ? 'contained' : undefined}
      data-studio-presentation={studioPresentation ? 'true' : undefined}
      data-project-state={state.kind}
      hidden={!active && !leaving}
      data-ground-phase={leaving ? 'leaving' : active && leavingGround ? 'entering' : undefined}
      aria-hidden={leaving ? 'true' : undefined}
      inert={leaving ? '' : undefined}
      role="region"
      aria-label="Project workspace"
    >
      <div className="ground-desk" style={windowStyle(win)} data-measured={win ? 'true' : 'false'}>
        {contained && (
          <header className="ground-board-header">
            <div>
              <h1 ref={containedHeadingRef} tabIndex={-1}>{START_BOARD_COPY.heading}</h1>
              {studioPresentation && mock && state.action?.disabled && <p className="start-board-project-caveat">{START_BOARD_COPY.projectDemoCaveat}</p>}
              <p>{state.kind === 'project' ? state.label : drawing?.name || state.drawingName}</p>
            </div>
            <button type="button" onClick={onReturnToDrawing}>{START_BOARD_COPY.returnToDrawing}</button>
          </header>
        )}
        {worldSpace ? (
          <WorldSpaceBoard key={workspaceProject?.project_id || 'anonymous'} scopeId={workspaceProject?.project_id || 'anonymous'} viewport={win} store={store}>
            {(renderTile) => <BoardTiles workspace={workspace} drawing={drawing} catalog={catalog} renderTile={renderTile} studioPresentation={studioPresentation} />}
          </WorldSpaceBoard>
        ) : (
          <div className="ground-tiles">
            <BoardTiles workspace={workspace} drawing={drawing} catalog={catalog} studioPresentation={studioPresentation} />
          </div>
        )}
        {mock && <p className="ground-note">Offline demo build: no workspace service stands behind this board.</p>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// iOS: the device stage. A phone silhouette whose screen carries the ship
// lane's state, derived by the SAME function IosSurface uses from the D-1
// contract (leaf.ios-ship-surface.v1); the three lane rungs are the real
// preconditions in order (a revision, mounted readiness, a launchable
// build) and light only from booleans the contract or the console holds.
// ---------------------------------------------------------------------------
export function DeviceGround({
  active = false, enabled = false, contract = null, projectLabel = null, revision = null,
  leavingGround = null,
}) {
  const state = enabled ? (deriveIosState(contract) ?? 'malformed') : 'dormant'
  const leaving = leavingGround === 'device-stage'
  const win = useGroundWindow(active || leaving)
  const label = state === 'dormant'
    ? 'Not available yet'
    : state === 'malformed' ? 'Status unreadable' : IOS_STATE_LABEL[state]
  const stage = state === 'in-progress' ? humanizeStage(contract?.build_stage) : null
  const rungs = [
    { id: 'revision', label: 'Approved revision', lit: Boolean(revision) },
    { id: 'readiness', label: 'Mounted Apple readiness', lit: state === 'ready' || state === 'in-progress' },
    { id: 'build', label: 'TestFlight build', lit: state === 'ready' },
  ]
  return (
    <div
      className="studio-ground-device"
      data-ground="ios"
      data-state={state}
      hidden={!active && !leaving}
      data-ground-phase={leaving ? 'leaving' : active && leavingGround ? 'entering' : undefined}
      aria-hidden={leaving ? 'true' : undefined}
      inert={leaving ? '' : undefined}
      role="region"
      aria-label="iOS ship lane"
    >
      <div className="ground-device-stage" style={windowStyle(win)} data-measured={win ? 'true' : 'false'}>
        <div className="device-frame">
          <span className="device-notch" aria-hidden="true" />
          <div className="device-screen">
            <span className="device-k">TestFlight lane</span>
            <strong className="device-v" data-testid="device-state">{label}</strong>
            {stage && <span className="device-stage">{stage}</span>}
            {(projectLabel || revision) && (
              <span className="device-meta">
                {projectLabel || 'project'}{revision ? ` · ${shortId(revision)}` : ''}
              </span>
            )}
          </div>
        </div>
        <div className="ground-device-side">
          <ol className="ground-lane" aria-label="Ship lane">
            {rungs.map((rung) => (
              <li key={rung.id} data-rung={rung.id} data-lit={rung.lit ? 'true' : 'false'} data-element-id={formatElementId('rung', rung.id) || undefined}>
                <span className={`dot ${rung.lit ? 'live' : 'hollow'}`} aria-hidden="true" />
                {rung.label}
              </li>
            ))}
          </ol>
          {/* Receipt identity is readiness detail: never shown while dormant. */}
          {state !== 'dormant' && contract?.receipt_id && (
            <p className="ground-note">
              receipt {shortId(contract.receipt_id, 12)}{contract.reported_at ? ` · ${contract.reported_at}` : ''}
            </p>
          )}
          {state === 'dormant' && <p className="ground-note">iOS setup status isn’t available yet.</p>}
          {state === 'never-configured' && <p className="ground-note">No ship-lane readiness has been published for this revision.</p>}
        </div>
      </div>
    </div>
  )
}

// Both non-drawing grounds, mounted once and toggled by `hidden`, so a tab
// switch never remounts a ground any more than it remounts the drawing.
export default function SurfaceGrounds({
  surface, workspaceProject, workspace, drawing, catalog, mock,
  boardVisible, onReturnToDrawing, headingRef, startFocusRequest, leavingGround = null,
  occluders = NO_OCCLUDERS,
  studioPresentation = false,
  iosEnabled, iosContract, revision,
}) {
  const projectLabel = workspaceProject?.kind === 'project'
    ? workspaceProject.label
    : workspaceProject?.drawingName || null
  return (
    <>
      {/* Slice 2: each ground is active for its DECLARED ground kind, not for
          a surface id (it used to compare the surface id to the browser and
          ios literals). An unknown surface still activates neither:
          surfaceGround falls closed to the CAD contract, whose ground is
          'drawing'. */}
      <ProjectBoardGround
        active={boardVisible ?? surfaceGround(surface) === 'board'}
        leavingGround={leavingGround}
        contained={boardVisible === true && groundShowsDrawing(surface)}
        occluders={occluders}
        onReturnToDrawing={onReturnToDrawing}
        headingRef={headingRef}
        startFocusRequest={startFocusRequest}
        studioPresentation={studioPresentation}
        workspaceProject={workspaceProject}
        workspace={workspace}
        drawing={drawing}
        catalog={catalog}
        mock={mock}
      />
      <DeviceGround
        active={!boardVisible && surfaceGround(surface) === 'device-stage'}
        leavingGround={leavingGround}
        enabled={iosEnabled}
        contract={iosContract}
        projectLabel={projectLabel}
        revision={revision}
      />
    </>
  )
}
