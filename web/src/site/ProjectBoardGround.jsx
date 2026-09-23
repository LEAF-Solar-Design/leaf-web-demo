import { useLayoutEffect, useRef, useState } from 'react'
import { readBoardTheme, writeBoardTheme } from '../lib/themePreference.js'
import WorldSpaceBoard from './WorldSpaceBoard.jsx'
import { BoardTiles } from './BoardTiles.jsx'
import { START_BOARD_COPY } from './startBoardCopy.js'
import { EMPTY_WORKSPACE_PROJECT } from './workspaceProjectState.js'
import { WorkspaceProjectSlot } from '../components/ProductSurfaceTabs.jsx'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from './groundWindow.js'

export function ProjectBoardGround({
  active = false, workspaceProject = null, workspace = null, drawing = null, catalog = null, mock = false,
  leavingGround = null,
  contained = false, onReturnToDrawing = null, headingRef = null, startFocusRequest = 0,
  themeable = !contained,
  onCreateProject = null,
  occluders = NO_OCCLUDERS,
  studioPresentation = false, studioShell = false,
  actions, panel,
  worldSpace = import.meta.env.VITE_WORLD_SPACE_BOARD === '1', store,
}) {
  const state = workspaceProject || EMPTY_WORKSPACE_PROJECT
  const [theme, setTheme] = useState(() => themeable ? readBoardTheme() : 'dark')
  const lightBoard = themeable && theme === 'light'
  const themeToggle = themeable && (
    <button
      type="button"
      className="ground-theme-toggle"
      aria-pressed={theme === 'light'}
      title={theme === 'light' ? 'Switch the board back to the dark theme' : 'Switch the board to the light paper theme'}
      onClick={() => {
        const next = theme === 'light' ? 'dark' : 'light'
        setTheme(next)
        writeBoardTheme(next)
      }}
    >Light board</button>
  )
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
  // In the shared shell the board owns its heading and measured window.
  // Outside it the legacy product frame still supplies Browser's heading.
  return (
    <div
      className={`studio-ground-board${lightBoard ? ' leaf-light' : ''}`}
      data-board-theme={lightBoard ? 'light' : undefined}
      ref={boardRef}
      data-ground="browser"
      data-studio-shell={studioShell ? 'cockpit' : undefined}
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
        {!contained && themeToggle}
        {contained && (
          <header className="ground-board-header">
            <div>
              <h1 ref={containedHeadingRef} tabIndex={-1}>{START_BOARD_COPY.heading}</h1>
              <p>{state.kind === 'project' ? state.label : drawing?.name || state.drawingName}</p>
              <WorkspaceProjectSlot state={state} onCreateProject={onCreateProject} studioPresentation={studioPresentation} mock={mock} />
            </div>
            {onReturnToDrawing && <button type="button" onClick={onReturnToDrawing}>{START_BOARD_COPY.returnToDrawing}</button>}
            {themeToggle}
          </header>
        )}
        {panel}
        {worldSpace ? (
          <WorldSpaceBoard key={workspaceProject?.project_id || 'anonymous'} scopeId={workspaceProject?.project_id || 'anonymous'} viewport={win} store={store}>
            {(renderTile) => <BoardTiles workspace={workspace} drawing={drawing} catalog={catalog} renderTile={renderTile} studioPresentation={studioPresentation} actions={actions} />}
          </WorldSpaceBoard>
        ) : (
          <div className="ground-tiles">
            <BoardTiles workspace={workspace} drawing={drawing} catalog={catalog} studioPresentation={studioPresentation} actions={actions} />
          </div>
        )}
        {mock && <p className="ground-note">Offline demo build: no workspace service stands behind this board.</p>}
      </div>
    </div>
  )
}
