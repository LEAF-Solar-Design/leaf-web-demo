import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ProjectMaterialIntake from './ProjectMaterialIntake.jsx'

afterEach(cleanup)
const project = { project_id: 'p1', name: 'Roof A' }
const target = { projectId: 'p1', projectName: 'Roof A', fileName: 'site.dwg' }
const upload = { policy: { enabled: true }, phase: 'idle' }

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
  rerender(<ProjectMaterialIntake project={{ name: 'Roof B' }} upload={upload} intake={{ phase: 'attached', target, drawing: { name: 'site.dwg', version: 2 } }} />)
  expect(screen.getByText('Attached site.dwg as version 2 to Roof A')).toBeTruthy()
  rerender(<ProjectMaterialIntake project={project} upload={upload} intake={{ phase: 'attach-failed', target, error: 'Import unavailable.' }} onRetry={retry} />)
  expect(screen.getByText('Import unavailable.')).toBeTruthy()
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
