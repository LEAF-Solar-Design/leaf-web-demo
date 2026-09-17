import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import ProjectWorkspacePanels, { PANE_NAMES, paneForCapability } from './ProjectWorkspacePanels.jsx'

afterEach(cleanup)
const project = { project_id: 'p1', name: 'North Yard' }

it('B2 row4 renders the controlled pane for the current project and returns to its board', () => {
  const onBack = vi.fn()
  const { container, rerender } = render(<ProjectWorkspacePanels pane={null} project={project} />)
  expect(container.innerHTML).toBe('')
  expect(Object.isFrozen(PANE_NAMES)).toBe(true)
  for (const pane of PANE_NAMES) {
    rerender(<ProjectWorkspacePanels pane={pane} project={project} onBack={onBack} mock />)
    expect(container.querySelector('.ground-pane')).toHaveAttribute('data-pane', pane)
    expect(screen.getByRole('heading', { name: pane[0].toUpperCase() + pane.slice(1), exact: true })).toBeInTheDocument()
    expect(screen.getByText(project.name)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
  }
  expect(onBack).toHaveBeenCalledTimes(PANE_NAMES.length)
  rerender(<ProjectWorkspacePanels pane="versions" project={{ name: 'South Yard' }} />)
  expect(screen.getByText('South Yard')).toBeInTheDocument()
  expect(screen.queryByText('North Yard')).toBeNull()
})

it('B2 row5 seats the job rail and shows bounded real job detail', () => {
  const job = { job_id: 'j1', tool_name: 'Measure', tool: 'Measure', status: 'complete', cost_usd: 0.25,
    input_version_id: 'v-in', output_version_id: 'v-out', created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:01:00Z', result: { count: 12 } }
  const onSelectJob = vi.fn()
  const props = { project, pane: 'jobs', workspace: { jobs: [job] }, currentJob: job, onSelectJob }
  const { container, rerender } = render(<ProjectWorkspacePanels {...props} />)
  const detail = within(container.querySelector('.ground-job-detail'))
  for (const value of ['complete', 'v-in', 'v-out', '$0.25', job.created_at, job.updated_at]) expect(detail.getByText(value)).toBeInTheDocument()
  expect(container.querySelector('.ground-job-detail pre').textContent).toBe(JSON.stringify(job.result, null, 2))
  fireEvent.click(within(container.querySelector('.rail')).getByRole('button', { name: /Measure/ }))
  expect(onSelectJob).toHaveBeenCalledWith(job)
  const result = { text: 'x'.repeat(5000) }
  rerender(<ProjectWorkspacePanels {...props} currentJob={{ ...job, result }} />)
  expect(container.querySelector('.ground-job-detail pre').textContent).toBe(`${JSON.stringify(result, null, 2).slice(0, 4000)}…`)
})

it('B2 row6 orders versions and preserves honest material and tool empties', () => {
  const old = { version_id: 'old', seq: 1, created_at: '2026-09-01' }
  const recent = { version_id: 'recent', seq: 2, created_at: '2026-09-02' }
  const onOpenVersion = vi.fn()
  const { rerender } = render(<ProjectWorkspacePanels project={project} pane="versions" workspace={{ drawing_versions: [old, recent] }} onOpenVersion={onOpenVersion} />)
  expect(screen.getAllByRole('listitem').map((row) => row.textContent)).toEqual(['v2 · recent', 'v1 · old'])
  fireEvent.click(screen.getByRole('button', { name: 'v2 · recent' }))
  expect(onOpenVersion).toHaveBeenCalledWith(recent)
  rerender(<ProjectWorkspacePanels project={project} pane="material" />)
  expect(screen.getByText('No material attached yet. Upload a drawing to attach it.')).toBeInTheDocument()
  rerender(<ProjectWorkspacePanels project={project} pane="tools" />)
  expect(screen.getByText('No built tools yet. Author one from the Manage tab.')).toBeInTheDocument()
  rerender(<ProjectWorkspacePanels project={project} pane="versions" />)
  expect(screen.getByText('No versions yet.')).toBeInTheDocument()
})

it('B2 row7 seats supplied elements and names unmounted panes', () => {
  const { rerender } = render(<ProjectWorkspacePanels project={project} pane="conversation" />)
  for (const pane of ['conversation', 'annotations', 'authoring', 'settings', 'catalog']) {
    rerender(<ProjectWorkspacePanels project={project} pane={pane} />)
    expect(screen.getByText(`${pane[0].toUpperCase() + pane.slice(1)} is not mounted on this surface.`)).toBeInTheDocument()
    rerender(<ProjectWorkspacePanels project={project} pane={pane} slots={{ [pane]: <div>Supplied work</div> }} />)
    expect(screen.getByText('Supplied work')).toBeInTheDocument()
    expect(screen.queryByText(/not mounted/)).toBeNull()
  }
})

it('B2 row8 maps all shared capabilities', () => {
  expect(['conversation', 'annotations', 'authoring', 'approvals', 'versions', 'receipts', 'marathons', 'one-shot execution'].map(paneForCapability))
    .toEqual(['conversation', 'annotations', 'authoring', 'annotations', 'versions', 'receipts', 'jobs', 'jobs'])
  expect(paneForCapability('unknown')).toBeNull()
  expect(paneForCapability('constructor')).toBeNull()
})
