/**
 * Surface grounds (W4a): each tab's ground renders the surface's REAL state
 * and says so honestly when there is none — never an invented project, job,
 * version, or ship-lane progress. Exactly one ground is visible per surface;
 * the others stay mounted but hidden.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import SurfaceGrounds, { DeviceGround, ProjectBoardGround, groundShowsDrawing, measureContainedWindow } from './SurfaceGrounds.jsx'
import { ProjectBoardGround as DirectProjectBoardGround } from './ProjectBoardGround.jsx'
import { DeviceGround as DirectDeviceGround } from '../ios/DeviceGround.jsx'
import { BoardTiles } from './BoardTiles.jsx'
import { deriveWorkspaceProjectState } from './workspaceProjectState.js'

afterEach(cleanup)

describe('extracted ground exports', () => {
  it('renders the same active board through either import', () => {
    const { container, rerender } = render(<DirectProjectBoardGround active worldSpace={false} />)
    const board = container.querySelector('[data-ground="browser"]')
    const markup = board.outerHTML
    rerender(<ProjectBoardGround active worldSpace={false} />)
    expect(container.querySelector('[data-ground="browser"]')).toBe(board)
    expect(board.outerHTML).toBe(markup)
  })

  it('renders the same active device stage through either import', () => {
    const { container, rerender } = render(<DirectDeviceGround active enabled revision="rev-1" />)
    const stage = container.querySelector('[data-ground="ios"]')
    const markup = stage.outerHTML
    rerender(<DeviceGround active enabled revision="rev-1" />)
    expect(container.querySelector('[data-ground="ios"]')).toBe(stage)
    expect(stage.outerHTML).toBe(markup)
  })

  it('exports the six board tiles', () => {
    const { container } = render(<BoardTiles />)
    expect(container.querySelectorAll('[data-tile]')).toHaveLength(6)
  })
})

it('themes both persistent grounds through the shell contract without a paper frame measurement', () => {
  const { container, rerender } = render(<SurfaceGrounds surface="browser" studioShell studioPresentation />)
  const board = container.querySelector('[data-ground="browser"]')
  const device = container.querySelector('[data-ground="ios"]')
  expect(board.getAttribute('data-studio-shell')).toBe('cockpit')
  expect(device.getAttribute('data-studio-shell')).toBe('cockpit')
  expect(board.getAttribute('data-board-layout')).toBe('contained')
  expect(board.hidden).toBe(false)
  rerender(<SurfaceGrounds surface="ios" studioShell studioPresentation />)
  expect(container.querySelector('[data-ground="browser"]')).toBe(board)
  expect(container.querySelector('[data-ground="ios"]')).toBe(device)
  expect(board.hidden).toBe(true)
  expect(device.hidden).toBe(false)
  const css = readFileSync(`${process.cwd()}/src/site/studioShell.css`, 'utf8')
  expect(css).toContain('[data-studio-shell="cockpit"]')
  expect(css).toContain('text-transform: none')
  expect(css).toContain('var(--ck-glass-panel)')
})

describe('W4g bleed-2b: ground crossfade accessibility', () => {
  it('paints the leaving board inertly and marks the active device entering', () => {
    const { container, rerender } = render(<SurfaceGrounds surface="ios" leavingGround="board" />)
    const board = container.querySelector('[data-ground="browser"]')
    const device = container.querySelector('[data-ground="ios"]')
    expect(board.hidden).toBe(false)
    expect(board.getAttribute('data-ground-phase')).toBe('leaving')
    expect(board.getAttribute('aria-hidden')).toBe('true')
    expect(board.hasAttribute('inert')).toBe(true)
    expect(device.getAttribute('data-ground-phase')).toBe('entering')
    expect(device.hasAttribute('aria-hidden')).toBe(false)
    expect(device.hasAttribute('inert')).toBe(false)
    rerender(<SurfaceGrounds surface="ios" />)
    expect(board.hidden).toBe(true)
    expect(device.hidden).toBe(false)
    for (const ground of [board, device]) {
      expect(ground.hasAttribute('data-ground-phase')).toBe(false)
      expect(ground.hasAttribute('inert')).toBe(false)
      expect(ground.hasAttribute('aria-hidden')).toBe(false)
    }
  })
  it('paints the leaving device inertly while the board enters', () => {
    const { container } = render(<SurfaceGrounds surface="browser" leavingGround="device-stage" />)
    const device = container.querySelector('[data-ground="ios"]')
    expect(device.hidden).toBe(false)
    expect(device.getAttribute('data-ground-phase')).toBe('leaving')
    expect(device.getAttribute('aria-hidden')).toBe('true')
    expect(device.hasAttribute('inert')).toBe(true)
    expect(container.querySelector('[data-ground="browser"]').getAttribute('data-ground-phase')).toBe('entering')
  })
})

const catalog = {
  families: [
    { family_id: 'measurement', label: 'Measurement', capabilities: [{ name: 'count-by-layer' }, { name: 'measure-panel-area' }] },
    { family_id: 'custom', label: 'Custom authored tools', capabilities: [{ name: 'delete-marked-panel' }] },
  ],
}

describe('groundShowsDrawing', () => {
  it('is true for the two CAD-shaped surfaces only', () => {
    expect(groundShowsDrawing('cad')).toBe(true)
    expect(groundShowsDrawing('solar')).toBe(true)
    expect(groundShowsDrawing('browser')).toBe(false)
    expect(groundShowsDrawing('ios')).toBe(false)
    expect(groundShowsDrawing(undefined)).toBe(false)
  })
})

describe('measureContainedWindow', () => {
  const board = { left: 0, top: 0, width: 1920, height: 940 }
  const element = (rect) => ({ getBoundingClientRect: () => rect })
  const rows = [
    ['header.top', 'top', [0, 0, 1920, 28]],
    ['#drafting-ribbon', 'top', [0, 28, 1920, 95]],
    ['.viewer-toolbar', 'top', [0, 123, 1920, 32]],
    ['[data-testid="cockpit-view"]', 'top', [250, 155, 1670, 26]],
    ['.properties-dock', 'left', [0, 155, 250, 754]],
    ['.bar-dock', 'bottom', [600, 880, 720, 25]],
    ['footer.foot-bar', 'bottom', [0, 909, 1920, 31]],
  ]

  it.each([
    ['the proof page', board, rows, { left: 266, top: 197, width: 1638, height: 667 }],
    ['the proof page with prompt reserve', board, rows.map((row) => row[0] === '.bar-dock' ? [...row, { reserve: 50 }] : row), { left: 266, top: 197, width: 1638, height: 617 }],
    ['the Properties pane closed', board, rows.filter(([selector]) => selector !== '.properties-dock'), { left: 16, top: 197, width: 1888, height: 667 }],
    ['a board without a box', { ...board, width: 0 }, rows, null],
    ['no occluders', board, [], { left: 16, top: 16, width: 1888, height: 908 }],
  ])('measures %s against the board box', (_name, boardRect, occluderRows, expected) => {
    const elements = Object.fromEntries(occluderRows.map(([selector, _edge, [left, top, width, height]]) => [
      selector, element({ left, top, width, height }),
    ]))
    const doc = { querySelector: (selector) => elements[selector] }
    const occluders = occluderRows.map(([selector, edge, _rect, options]) => [selector, edge, options])
    expect(measureContainedWindow(element(boardRect), doc, occluders)).toEqual(expected)
  })

  it('returns null without a board', () => {
    expect(measureContainedWindow(null)).toBeNull()
  })
})

describe('ProjectBoardGround', () => {
  it('repositions the contained desk when Properties mounts late', async () => {
    let frame
    const animationFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frame = callback
      return 1
    })
    const occluders = [['.viewer-toolbar', 'top'], ['.bar-dock', 'bottom'], ['.properties-dock', 'left']]
    try {
      const { container, unmount } = render(
        <div className="app">
          <div className="viewer-toolbar" />
          <div className="bar-dock" />
          <ProjectBoardGround active contained occluders={occluders} />
        </div>,
      )
      container.querySelector('[data-ground="browser"]').getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 900 })
      container.querySelector('.viewer-toolbar').getBoundingClientRect = () => ({ left: 0, top: 60, width: 1000, height: 40 })
      container.querySelector('.bar-dock').getBoundingClientRect = () => ({ left: 0, top: 700, width: 1000, height: 60 })
      fireEvent(window, new Event('resize'))
      act(() => frame())
      const desk = container.querySelector('.ground-desk')
      expect(desk).toHaveStyle({ top: '116px', left: '16px', width: '968px', height: '568px' })

      frame = undefined
      const properties = document.createElement('div')
      properties.className = 'properties-dock'
      properties.getBoundingClientRect = () => ({ left: 0, top: 100, width: 250, height: 600 })
      container.querySelector('.app').append(properties)
      await act(async () => {})
      expect(frame).toBeTypeOf('function')
      act(() => frame())
      expect(desk).toHaveStyle({ top: '116px', left: '266px', width: '718px', height: '568px' })
      unmount()
    } finally {
      animationFrame.mockRestore()
    }
  })

  it('describes each catalog tool and its declared drawing effect only in the studio presentation', () => {
    const tools = { families: [{ family_id: 'measurement', label: 'Measurement', capabilities: [
      { name: 'edit', label: 'Edit panels', description: 'Moves the selected panels.', capabilities: ['drawing.read', 'drawing.write'] },
      { name: 'measure', capabilities: ['drawing.read'] },
      { name: 'unknown', description: 'Checks a service.' },
    ] }] }
    const { container, rerender } = render(<ProjectBoardGround active catalog={tools} studioPresentation />)
    const tile = within(container.querySelector('[data-tile="catalog"]'))
    expect(tile.getByText('Measurement')).toHaveAttribute('data-element-id', 'family:measurement')
    expect(tile.getByText('Edit panels')).toBeInTheDocument()
    expect(tile.getByText('Moves the selected panels.')).toBeInTheDocument()
    expect(tile.getByText('measure')).toBeInTheDocument()
    expect(tile.getByText('unknown')).toBeInTheDocument()
    for (const text of ['Changes the drawing', 'Does not change the drawing', 'No description provided.', 'Drawing effect not specified.']) {
      expect(tile.getByText(text)).toBeInTheDocument()
    }
    rerender(<ProjectBoardGround active catalog={tools} />)
    expect(tile.queryByText('Edit panels')).toBeNull()
  })

  it('offers project creation from the contained board with the derived drawing name', () => {
    const workspaceProject = deriveWorkspaceProjectState({ drawingName: 'North Yard', orgId: 'org-1' })
    const onCreateProject = vi.fn()
    render(<ProjectBoardGround active contained workspaceProject={workspaceProject} onCreateProject={onCreateProject} />)
    const button = screen.getByRole('button', { name: 'Create project from this drawing' })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(onCreateProject).toHaveBeenCalledTimes(1)
    expect(onCreateProject).toHaveBeenCalledWith(workspaceProject.action.projectName)
  })

  it('disables project creation in the contained offline demo and explains the limit once', () => {
    const workspaceProject = deriveWorkspaceProjectState({ drawingName: 'demo', mock: true })
    const { container } = render(<ProjectBoardGround active contained mock studioPresentation workspaceProject={workspaceProject} />)
    expect(screen.getByRole('button', { name: 'Create project from this drawing' })).toBeDisabled()
    expect(screen.getAllByText('Creating a workspace project is unavailable in this offline demo.')).toHaveLength(1)
    expect(container.querySelector('.start-board-project-caveat')).toBeNull()
  })

  it.each(['cad', 'solar'])('focuses each %s Start request once, after measurement', (surface) => {
    let frame
    const animationFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frame = callback
      return 1
    })
    const occluders = [['.viewer-toolbar', 'top'], ['.bar-dock', 'bottom']]
    const view = (request, boardCatalog = null) => (
      <div className="app">
        <div className="viewer-toolbar" />
        <main className="center-scroll" />
        <div className="bar-dock"><button type="button">Other focus</button></div>
        <SurfaceGrounds surface={surface} boardVisible startFocusRequest={request} catalog={boardCatalog} occluders={occluders} />
      </div>
    )
    try {
      const { container, rerender } = render(view(1))
      const heading = screen.getByRole('heading', { level: 1, name: 'Project board' })
      const focus = vi.spyOn(heading, 'focus')
      expect(container.querySelector('.ground-desk')).toHaveAttribute('data-measured', 'false')
      expect(heading).not.toHaveFocus()
      expect(document.activeElement).toBe(document.body)

      const toolbar = container.querySelector('.viewer-toolbar')
      const prompt = container.querySelector('.bar-dock')
      const board = container.querySelector('[data-ground="browser"]')
      board.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 900 })
      toolbar.getBoundingClientRect = () => ({ left: 0, top: 60, width: 1000, height: 40 })
      prompt.getBoundingClientRect = () => ({ left: 0, top: 700, width: 1000, height: 60 })
      fireEvent(window, new Event('resize'))
      act(() => frame())
      expect(container.querySelector('.ground-desk')).toHaveAttribute('data-measured', 'true')
      expect(container.querySelector('.ground-desk')).toHaveStyle({ top: '116px', left: '16px', width: '968px', height: '568px' })
      expect(heading).toHaveFocus()
      expect(focus).toHaveBeenCalledTimes(1)

      const other = screen.getByRole('button', { name: 'Other focus' })
      other.focus()
      rerender(view(1, catalog))
      prompt.getBoundingClientRect = () => ({ left: 0, top: 650, width: 1000, height: 60 })
      fireEvent(window, new Event('resize'))
      act(() => frame())
      expect(container.querySelector('.ground-desk')).toHaveStyle({ top: '116px', left: '16px', width: '968px', height: '518px' })
      expect(other).toHaveFocus()
      expect(focus).toHaveBeenCalledTimes(1)

      rerender(view(2, catalog))
      expect(heading).toHaveFocus()
      expect(focus).toHaveBeenCalledTimes(2)
    } finally {
      animationFrame.mockRestore()
    }
  })

  it('keeps the contained desk unmeasured without a measurable board', () => {
    const { container } = render(<SurfaceGrounds surface="cad" boardVisible />)
    const board = container.querySelector('[data-ground="browser"]')
    expect(board).toHaveAttribute('data-board-layout', 'contained')
    expect(board.querySelector('.ground-desk')).toHaveAttribute('data-measured', 'false')
  })

  it.each(['cad', 'solar'])('opens the same board in %s with its own heading and return action', (surface) => {
    const onReturnToDrawing = vi.fn()
    const view = (boardVisible) => <SurfaceGrounds surface={surface} boardVisible={boardVisible} onReturnToDrawing={onReturnToDrawing} />
    const { container, rerender } = render(view(false))
    const board = container.querySelector('[data-ground="browser"]')
    expect(board).toHaveAttribute('hidden')
    rerender(view(true))
    expect(container.querySelector('[data-ground="browser"]')).toBe(board)
    expect(board).not.toHaveAttribute('hidden')
    expect(board).toHaveAttribute('data-board-layout', 'contained')
    const heading = within(board).getByRole('heading', { level: 1, name: 'Project board' })
    expect(heading).toHaveAttribute('tabindex', '-1')
    expect(within(board).getAllByRole('heading', { level: 1 })).toHaveLength(1)
    fireEvent.click(within(board).getByRole('button', { name: 'Return to drawing' }))
    expect(onReturnToDrawing).toHaveBeenCalledTimes(1)
    rerender(view(false))
    expect(container.querySelector('[data-ground="browser"]')).toBe(board)
    expect(board).toHaveAttribute('hidden')
    expect(board).not.toHaveAttribute('data-board-layout')
  })

  it('renders the honest empties with no project, no drawing, and no catalog yet', () => {
    render(<ProjectBoardGround active />)
    const board = screen.getByRole('region', { name: 'Project workspace' })
    expect(board).not.toHaveAttribute('hidden')
    expect(board.dataset.projectState).toBe('empty')
    // The frame's chrome names the project state above the window; the board
    // never repeats it (data-project-state is the board's own record).
    expect(within(board).queryByRole('heading', { level: 2 })).toBeNull()
    expect(within(board).getByText('No drawing mounted')).toBeTruthy()
    expect(within(board).getByText('Versions live with a workspace project')).toBeTruthy()
    expect(within(board).getByText('Runs appear here with a project open')).toBeTruthy()
    expect(within(board).getByText('Loading the live catalog')).toBeTruthy()
    // Nothing invented: no version, job, or tool rows exist.
    expect(within(board).queryAllByRole('listitem').map((li) => li.textContent))
      .not.toContain(expect.stringMatching(/^v\d/))
  })

  it('renders the mounted drawing and the open project\'s real objects', () => {
    const workspaceProject = deriveWorkspaceProjectState({
      openProjectId: 'proj-1', projectName: 'North Yard', drawingName: 'rooftop_demo', orgId: 'org-1',
    })
    const workspace = {
      drawing_versions: [
        { version_id: 'v-1', seq: 1, drawing_id: 'abcdef123456' },
        { version_id: 'v-2', seq: 2, drawing_id: 'abcdef123456' },
      ],
      jobs: [
        { job_id: 'j-1', tool_name: 'count-by-layer', status: 'complete' },
        { job_id: 'j-2', tool_name: 'measure-panel-area', status: 'running' },
      ],
      built_tools: [{ tool_id: 't-1', name: 'delete-marked-panel' }],
    }
    render(
      <ProjectBoardGround
        active
        workspaceProject={workspaceProject}
        workspace={workspace}
        drawing={{ name: 'rooftop_demo', polylines: 2345, layers: 4 }}
        catalog={catalog}
      />,
    )
    const board = screen.getByRole('region', { name: 'Project workspace' })
    expect(board.dataset.projectState).toBe('project')
    expect(within(board).getByText('2345 polylines · 4 layers')).toBeTruthy()
    expect(within(board).getByText('2 drawing versions')).toBeTruthy()
    // Newest job first, and the running one is named as running.
    const jobs = within(within(board).getByRole('region', { name: 'Jobs' })).getAllByRole('listitem')
    expect(jobs[0].textContent).toContain('measure-panel-area')
    expect(jobs[0].textContent).toContain('running')
    expect(within(board).getByText('delete-marked-panel')).toBeTruthy()
    expect(within(board).getByText('2 families · 3 tools')).toBeTruthy()
    expect(within(board).queryByText(/No project open/)).toBeNull()
  })

  it('names the drawing-only state without inventing a project', () => {
    const workspaceProject = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', mock: true })
    render(<ProjectBoardGround active workspaceProject={workspaceProject} mock />)
    const board = screen.getByRole('region', { name: 'Project workspace' })
    expect(board.dataset.projectState).toBe('drawing-only')
    expect(within(board).getByText(/Offline demo build/)).toBeTruthy()
    // jsdom has no layout: measureGroundWindow() can't place the window, so
    // this exercises the exact unmeasured state landing.css must hide (see
    // 'hides the ground desk when the window can't be measured' below) —
    // real browsers only reach this state transiently, jsdom reaches it
    // always, and either way a rule keyed on this attribute is what keeps it
    // from painting over the frame's own head text.
    expect(board.querySelector('.ground-desk').dataset.measured).toBe('false')
  })

  it('is hidden, not unmounted, when inactive', () => {
    const { container } = render(<ProjectBoardGround active={false} />)
    const board = container.querySelector('[data-ground="browser"]')
    expect(board).not.toBeNull()
    expect(board).toHaveAttribute('hidden')
  })
})

describe('DeviceGround', () => {
  const contract = (readiness, buildStage = null) => ({
    schema: 'leaf.ios-ship-surface.v1', project_id: 'proj-1', revision: 'rev-1',
    readiness, build_stage: buildStage, receipt_id: 'rcpt-0123456789abcdef', reported_at: '2026-09-02T03:00:00Z',
  })

  it('stays dormant with the surface flag off — no readiness detail at all', () => {
    render(<DeviceGround active enabled={false} contract={contract({ healthy: true, launchable: true })} />)
    const stage = screen.getByRole('region', { name: 'iOS ship lane' })
    expect(stage.dataset.state).toBe('dormant')
    expect(screen.getByTestId('device-state').textContent).toBe('Not available yet')
    expect(within(stage).queryByText(/receipt/)).toBeNull()
    for (const li of within(stage).getAllByRole('listitem')) expect(li.dataset.lit).toBe('false')
  })

  it('derives the four contract states exactly as IosSurface does, and lights the lane from booleans only', () => {
    const { rerender } = render(<DeviceGround active enabled contract={null} revision="rev-1" />)
    expect(screen.getByRole('region', { name: 'iOS ship lane' }).dataset.state).toBe('never-configured')
    expect(screen.getByTestId('device-state').textContent).toBe('Not yet configured')
    expect(within(screen.getByRole('list', { name: 'Ship lane' })).getAllByRole('listitem').map((li) => li.dataset.lit))
      .toEqual(['true', 'false', 'false'])

    rerender(<DeviceGround active enabled contract={contract({ healthy: true, launchable: false }, 'MAC_ALLOCATED')} revision="rev-1" />)
    expect(screen.getByRole('region', { name: 'iOS ship lane' }).dataset.state).toBe('in-progress')
    expect(screen.getByText('Mac allocated')).toBeTruthy()
    expect(within(screen.getByRole('list', { name: 'Ship lane' })).getAllByRole('listitem').map((li) => li.dataset.lit))
      .toEqual(['true', 'true', 'false'])
    // No percentage, no bar: the contract has no progress field.
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(screen.queryByText(/%/)).toBeNull()

    rerender(<DeviceGround active enabled contract={contract({ healthy: true, launchable: true })} revision="rev-1" projectLabel="North Yard" />)
    expect(screen.getByRole('region', { name: 'iOS ship lane' }).dataset.state).toBe('ready')
    expect(within(screen.getByRole('list', { name: 'Ship lane' })).getAllByRole('listitem').map((li) => li.dataset.lit))
      .toEqual(['true', 'true', 'true'])
    expect(screen.getByText(/receipt rcpt-0123456/)).toBeTruthy()
    expect(screen.getByText('North Yard · rev-1')).toBeTruthy()

    rerender(<DeviceGround active enabled contract={contract({ healthy: false, launchable: false })} />)
    expect(screen.getByRole('region', { name: 'iOS ship lane' }).dataset.state).toBe('unavailable')
  })

  it('renders a malformed contract as unreadable rather than guessing', () => {
    render(<DeviceGround active enabled contract={{ readiness: { healthy: 'yes' } }} />)
    expect(screen.getByRole('region', { name: 'iOS ship lane' }).dataset.state).toBe('malformed')
    expect(screen.getByTestId('device-state').textContent).toBe('Status unreadable')
  })
})

describe('SurfaceGrounds', () => {
  it('shows exactly the active surface\'s ground and keeps the other mounted but hidden', () => {
    const { container, rerender } = render(<SurfaceGrounds surface="browser" catalog={catalog} />)
    const board = () => container.querySelector('[data-ground="browser"]')
    const device = () => container.querySelector('[data-ground="ios"]')
    expect(board()).not.toHaveAttribute('hidden')
    expect(device()).toHaveAttribute('hidden')
    rerender(<SurfaceGrounds surface="ios" catalog={catalog} />)
    expect(board()).toHaveAttribute('hidden')
    expect(device()).not.toHaveAttribute('hidden')
    rerender(<SurfaceGrounds surface="cad" catalog={catalog} />)
    expect(board()).toHaveAttribute('hidden')
    expect(device()).toHaveAttribute('hidden')
    // Same nodes across switches: hidden, never remounted.
    const boardNode = board()
    rerender(<SurfaceGrounds surface="browser" catalog={catalog} />)
    expect(board()).toBe(boardNode)
  })
})

describe('ground window fallback geometry', () => {
  // measureGroundWindow() (this file) places the window from the real DOM;
  // --ground-top/left/right/window (landing.css) is only a fixed stand-in
  // for when that measurement can't run yet or came back too small. That
  // stand-in has no idea which project state is showing (the chrome grows
  // taller on some — an explainer, an action, a reason) or whether the page
  // has scrolled, so it is a guess, not a placement: painting ground tiles
  // at a guessed position risks landing them over the frame's own head
  // text, which is the collision this whole desk/window split exists to
  // avoid. The desk marks the guess with data-measured="false"
  // (ProjectBoardGround/DeviceGround above); this asserts landing.css
  // actually keeps that guess invisible rather than painting it.
  it('hides the ground desk and device stage when the window could not be measured', () => {
    const css = readFileSync(`${process.cwd()}/src/site/landing.css`, 'utf8')
    expect(css).toMatch(/\.ground-desk\[data-measured="false"\][^{]*\{[^}]*visibility:\s*hidden;/s)
    expect(css).toMatch(/\.ground-device-stage\[data-measured="false"\][^{]*\{[^}]*visibility:\s*hidden;/s)
  })
})
