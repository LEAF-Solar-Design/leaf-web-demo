import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ProjectMaterialIntake from './ProjectMaterialIntake.jsx'
import { createMaterialIntakeController } from './createMaterialIntakeController.js'

afterEach(cleanup)
const project = { project_id: 'p1', name: 'Roof A' }
const target = { projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' }
const upload = { policy: { enabled: true }, phase: 'idle' }

// Public DrawingVersion.to_dict() shape returned by the normal import route.
// Drawing name belongs to the upload target, not to this version record.
const drawingVersion = {
  version_id: '11111111-1111-4111-8111-111111111111',
  drawing_id: '22222222-2222-4222-8222-222222222222', project_id: 'p1', org_id: 'o1', seq: 2,
  oss_object: 'fixture.dwg', intake_ref: 'fixture.intake.json', created_by: 'fixture-actor',
  provenance: { schema: 'leaf.drawing-import.v1', source: { kind: 'account_upload', version: 2 } },
  idempotency_key: 'fixture-import', created_at: '2026-09-17T12:00:00Z',
  deleted_at: null, purge_requested_at: null, purge_completed_at: null,
}

it('B3 row8 renders phase sentences, retry, disabled intake and material lists', () => {
  const retry = vi.fn()
  const { rerender, container } = render(<ProjectMaterialIntake project={project} upload={upload} intake={{ phase: 'idle' }} />)
  expect(container.querySelector('section.ground-pane[data-pane="material-intake"]')).toBeTruthy()
  expect(screen.getByText('Material attaches to Roof A')).toBeTruthy()
  expect(screen.queryByRole('list')).toBeNull()
  for (const [phase, sentence] of [['uploading', 'Uploading site.dwg'], ['extracting', 'Extracting site.dwg'], ['failed', 'Extraction failed.']]) {
    rerender(<ProjectMaterialIntake project={project} upload={{ ...upload, phase, error: phase === 'failed' ? 'Extraction failed.' : null }} intake={{ phase: 'pending', target }} />)
    expect(screen.getByRole('status').textContent).toBe(sentence)
  }
  rerender(<ProjectMaterialIntake project={project} upload={upload} intake={{ phase: 'attaching', target }} />)
  expect(screen.getByText('Attaching to Roof A')).toBeTruthy()
  rerender(<ProjectMaterialIntake project={project} upload={upload} intake={{ phase: 'pending', target, beginRefused: 'Wait for the current upload to finish.' }} />)
  expect(screen.getByText('Wait for the current upload to finish.')).toBeTruthy()
  rerender(<ProjectMaterialIntake project={{ name: 'Roof B' }} upload={upload} intake={{ phase: 'attached', target, drawing: drawingVersion }} />)
  expect(screen.getByText('Attached site.dwg as version 2 to Roof A')).toBeTruthy()
  rerender(<ProjectMaterialIntake project={project} upload={upload} intake={{ phase: 'attach-failed', target, error: 'Import unavailable.' }} onRetry={retry} />)
  expect(screen.getByRole('status').textContent).toBe('Import unavailable.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(retry).toHaveBeenCalledTimes(1)
  rerender(<ProjectMaterialIntake upload={upload} />)
  expect(screen.getByText('Open a project first.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Upload DWG or DXF' }).disabled).toBe(true)
  expect(screen.getByLabelText('Drawing file').disabled).toBe(true)
  rerender(<ProjectMaterialIntake project={project} upload={upload} artifacts={[]} />)
  expect(screen.getByText('No material yet. Upload a DWG or DXF.')).toBeTruthy()
  const created = '2026-09-17T12:00:00Z'
  rerender(<ProjectMaterialIntake project={project} upload={upload} artifacts={[{ drawing_id: 'd1', name: 'Survey', status: 'ready', created_at: created }]} />)
  expect(screen.getByRole('list', { name: 'Project material' })).toBeTruthy()
  expect(screen.getByText('Survey')).toBeTruthy()
  expect(screen.getByText('ready')).toBeTruthy()
  expect(screen.getByText(new Date(created).toLocaleDateString())).toBeTruthy()
})

it('renders the public import sequence through the real material controller', async () => {
  const controller = createMaterialIntakeController({ services: {
    importUpload: vi.fn(async () => ({ drawingVersion, replayed: false })),
  } })
  controller.begin(target)
  await controller.onUploadReady({ receipt: { drawing_id: 'source-upload' }, status: { extracted_version: 2 } })
  expect(controller.getSnapshot().drawing).toBe(drawingVersion)
  expect(drawingVersion).not.toHaveProperty('version')
  expect(drawingVersion).not.toHaveProperty('name')
  render(<ProjectMaterialIntake project={project} upload={upload} intake={controller.getSnapshot()} />)
  expect(screen.getByRole('status').textContent).toBe('Attached site.dwg as version 2 to Roof A')
  controller.dispose()
})

it('confirms attachment without inventing a sequence when it is unavailable', () => {
  const { seq: _sequence, ...withoutSequence } = drawingVersion
  render(<ProjectMaterialIntake project={project} upload={upload}
    intake={{ phase: 'attached', target, drawing: withoutSequence }} />)
  expect(screen.getByRole('status').textContent).toBe('Attached site.dwg to Roof A')
  expect(screen.getByRole('status').textContent).not.toMatch(/undefined|null|version/)
})

it('B3 row9 forwards the selected file through the upload control', () => {
  const start = vi.fn()
  const { rerender } = render(<ProjectMaterialIntake project={project} upload={upload} onStartUpload={start} />)
  const file = new File(['drawing'], 'site.dwg')
  fireEvent.change(screen.getByLabelText('Drawing file'), { target: { files: [file] } })
  expect(start).toHaveBeenCalledExactlyOnceWith(file)
  rerender(<ProjectMaterialIntake upload={upload} onStartUpload={start} />)
  fireEvent.change(screen.getByLabelText('Drawing file'), { target: { files: [file] } })
  expect(start).toHaveBeenCalledTimes(1)
})

it('keeps demo upload controls disabled with or without a project', () => {
  const start = vi.fn()
  const { rerender } = render(<ProjectMaterialIntake mock upload={upload} onStartUpload={start} />)
  for (const selectedProject of [null, project]) {
    rerender(<ProjectMaterialIntake mock project={selectedProject} upload={upload} onStartUpload={start} />)
    expect(screen.getByLabelText('Drawing file').disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Upload DWG or DXF' }).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Drawing file'), { target: { files: [new File(['drawing'], 'site.dwg')] } })
    expect(start).not.toHaveBeenCalled()
    expect(screen.getByText('Uploads are unavailable in this demo.')).toBeTruthy()
    expect(screen.queryByText('Open a project first.')).toBeNull()
    expect(screen.queryByText('No material yet. Upload a DWG or DXF.')).toBeNull()
  }
})
