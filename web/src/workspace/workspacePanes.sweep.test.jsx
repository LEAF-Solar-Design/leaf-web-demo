import React, { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ProjectMaterialIntake from './ProjectMaterialIntake.jsx'
import ProjectWorkspacePanels from './ProjectWorkspacePanels.jsx'
import WorkspaceSummary from '../components/WorkspaceSummary.jsx'
import { StandaloneMaterialUpload } from '../App.jsx'
import useDrawingUploadController from '../controllers/upload/useDrawingUploadController.js'
import { deriveWorkspaceProjectState } from '../site/workspaceProjectState.js'
import { createWorkspaceController } from '../controllers/workspace/createWorkspaceController.js'

vi.mock('../controllers/upload/useDrawingUploadController.js', () => ({ default: vi.fn() }))

afterEach(cleanup)
const project = { project_id: 'p1', name: 'Roof' }

function standaloneMaterial(upload) {
  useDrawingUploadController.mockReturnValue(upload)
  return StandaloneMaterialUpload
}

function readyUpload() {
  const snapshot = { policyLoading: false, policy: { enabled: true }, busy: false, phase: 'idle' }
  return { ...snapshot, getSnapshot: () => snapshot,
    actions: { cancel: vi.fn(), upload: vi.fn(), setEngine: vi.fn() } }
}

it('S1-145 presents one safe upload error with a recovery and closed diagnostics', () => {
  const { rerender, container } = render(<ProjectMaterialIntake project={project} />)
  for (const error of ['HTTP 500', 'invalid_response', 'Failed to fetch', 'POST /api/drawings/upload -> 502']) {
    rerender(<ProjectMaterialIntake project={project} upload={{ phase: 'failed', error }} />)
    expect(screen.getByRole('status').textContent).not.toContain(error)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Choose the drawing again to retry the upload.')).toBeTruthy()
    expect(container.querySelector('details').open).toBe(false)
    expect(container.querySelector('details pre').textContent).toBe(error)
  }
})

it('S1-145 uses the same safe presenter for attachment failures', () => {
  const retry = vi.fn()
  render(<ProjectMaterialIntake project={project} upload={{ phase: 'failed', error: 'Failed to fetch' }}
    intake={{ phase: 'attach-failed', error: 'HTTP 500' }} onRetry={retry} />)
  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(screen.getByRole('status').textContent).not.toContain('HTTP')
  expect(screen.queryByText('Failed to fetch')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(retry).toHaveBeenCalledOnce()
})

it('S1-147 shows only sample material guidance in the demo', () => {
  render(<ProjectWorkspacePanels pane="material" mock />)
  expect(screen.getByText(/This demo uses sample drawing material/)).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Open workspace' }).getAttribute('href')).toBe('/app')
  expect(screen.getByRole('button', { name: 'Upload DWG or DXF' }).disabled).toBe(true)
  expect(screen.getByLabelText('Drawing file').disabled).toBe(true)
  expect(screen.getByText('Uploads are unavailable in this demo.')).toBeTruthy()
  expect(screen.queryByText(/Upload a drawing to attach/)).toBeNull()
  expect(screen.queryByText('Open a project first.')).toBeNull()
})

it('S1-148 lets the mounted intake own the only material list and dates', () => {
  const artifacts = [{ drawing_id: 'd1', name: 'Survey', created_at: '2026-09-17T12:00:00Z' }]
  const { container } = render(<ProjectWorkspacePanels pane="material" project={project}
    workspace={{ drawing_artifacts: artifacts }} slots={{ material: <ProjectMaterialIntake
      project={project} artifacts={artifacts} /> }} />)
  expect(screen.getAllByText('Survey')).toHaveLength(1)
  expect(screen.getAllByRole('list')).toHaveLength(1)
  expect(screen.getByRole('button', { name: 'Upload DWG or DXF' })).toBeTruthy()
  expect(container.querySelector('time').textContent).toBe(new Date(artifacts[0].created_at).toLocaleDateString())
})

it('S1-149 focuses the pane heading and returns to the originating tile', () => {
  function Board() {
    const [pane, setPane] = useState(null)
    return <><ProjectWorkspacePanels pane={pane} onBack={() => setPane(null)} mock />
      <button onClick={() => setPane('material')}>Material tile</button></>
  }
  render(<Board />)
  const tile = screen.getByRole('button', { name: 'Material tile' })
  tile.focus()
  fireEvent.click(tile)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Material' }))
  fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
  expect(document.activeElement).toBe(tile)
})

it('S1-149 focuses standalone Material and restores each opening tile after Back', () => {
  const upload = readyUpload()
  const StandaloneMaterialUpload = standaloneMaterial(upload)
  const onBack = vi.fn()
  function Board() {
    const [pane, setPane] = useState(null)
    return <><StandaloneMaterialUpload token="standalone" pane={pane} selection={null}
      getContext={() => ({ token: 'standalone', pane, selection: null })}
      onPolicy={() => {}} onReady={() => {}} onBack={() => { onBack(); setPane(null) }} />
      <button onClick={() => setPane('material')}>Material tile</button>
      <button onClick={() => setPane('material')}>Another Material tile</button></>
  }
  render(<Board />)
  for (const name of ['Material tile', 'Another Material tile']) {
    const tile = screen.getByRole('button', { name })
    tile.focus()
    fireEvent.click(tile)
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Material' }))
    expect(screen.getByRole('button', { name: 'Upload DWG or DXF' }).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
    expect(document.activeElement).toBe(tile)
    expect(screen.queryByRole('heading', { name: 'Material' })).toBeNull()
  }
  expect(upload.actions.cancel).toHaveBeenCalledTimes(2)
  expect(onBack).toHaveBeenCalledTimes(2)
})

it('S1-149 restores standalone Material focus on pane changes and unmount', () => {
  const StandaloneMaterialUpload = standaloneMaterial(readyUpload())
  const { unmount: removeTile } = render(<button>Material tile</button>)
  const tile = screen.getByRole('button', { name: 'Material tile' })
  const props = { token: 'standalone', selection: null,
    getContext: () => ({ token: 'standalone', pane: 'material', selection: null }),
    onPolicy: () => {}, onReady: () => {}, onBack: () => {} }
  tile.focus()
  const { rerender, unmount } = render(<StandaloneMaterialUpload {...props} pane="material" />)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Material' }))
  rerender(<StandaloneMaterialUpload {...props} pane="versions" />)
  expect(document.activeElement).toBe(tile)
  rerender(<StandaloneMaterialUpload {...props} pane="material" />)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Material' }))
  unmount()
  expect(document.activeElement).toBe(tile)
  removeTile()
})

it('S1-202 exposes opening and failed-open recovery in the body', () => {
  const retry = vi.fn(), close = vi.fn()
  const { rerender } = render(<WorkspaceSummary loading onClose={close} />)
  expect(screen.getByRole('status').textContent).toBe('Opening project...')
  rerender(<WorkspaceSummary error="GET /api/projects/private -> 500" onRetry={retry} onClose={close} />)
  expect(screen.getByRole('alert').textContent).not.toContain('/api/')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(retry).toHaveBeenCalledOnce()
  expect(close).toHaveBeenCalledOnce()
})

it('S1-202 does not call a listed project open before hydration succeeds', () => {
  const input = { openProjectId: 'p1', projectName: 'Roof', workspace: null }
  expect(deriveWorkspaceProjectState({ ...input, workspaceLoading: true }).kind).toBe('opening')
  expect(deriveWorkspaceProjectState({ ...input, workspaceError: 'failed' }).kind).toBe('open-failed')
  expect(deriveWorkspaceProjectState({ ...input, workspace: { project } }).kind).toBe('project')
})

it('S1-202 retains a retry target and clears the failed-open state on retry and close', async () => {
  const openProject = vi.fn().mockRejectedValueOnce(new Error('HTTP 500')).mockResolvedValue({ project })
  const controller = createWorkspaceController({ services: { openProject }, storage: null })
  await controller.openProject('p1')
  expect(controller.getSnapshot()).toMatchObject({ openProjectId: 'p1', workspace: null,
    workspaceLoading: false, workspaceError: 'Something went wrong. Try again.' })
  const retry = controller.openProject('p1')
  expect(controller.getSnapshot().workspaceError).toBeNull()
  await retry
  expect(controller.getSnapshot().workspace).toEqual({ project })
  controller.closeProject()
  expect(controller.getSnapshot()).toMatchObject({ openProjectId: null, workspaceError: null })
})
