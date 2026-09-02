/**
 * Surface grounds (W4a): each tab's ground renders the surface's REAL state
 * and says so honestly when there is none — never an invented project, job,
 * version, or ship-lane progress. Exactly one ground is visible per surface;
 * the others stay mounted but hidden.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import SurfaceGrounds, { DeviceGround, ProjectBoardGround, groundShowsDrawing } from './SurfaceGrounds.jsx'
import { deriveWorkspaceProjectState } from './workspaceProjectState.js'

afterEach(cleanup)

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

describe('ProjectBoardGround', () => {
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
