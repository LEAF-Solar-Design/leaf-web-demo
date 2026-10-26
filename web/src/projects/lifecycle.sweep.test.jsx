import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CloneDialog from './CloneDialog.jsx'
import DangerZone from './DangerZone.jsx'
import ExportDialog from './ExportDialog.jsx'
import ProjectLifecyclePanel from './ProjectLifecyclePanel.jsx'
import { cloneProject, getProjectLifecycle } from './api.js'

vi.mock('./api.js', () => ({
  cloneProject: vi.fn(), deleteProject: vi.fn(), exportProject: vi.fn(),
  getOrgIdentities: vi.fn(), getProjectLifecycle: vi.fn(), inviteMember: vi.fn(),
  resetProject: vi.fn(), revokeMember: vi.fn(), setIdentityDisplayName: vi.fn(),
}))

const project = { project_id: 'project-a', name: 'Roof' }
const snapshot = { project, members: [], receipts: [], files: [], viewer: null }

beforeEach(() => {
  vi.resetAllMocks()
  getProjectLifecycle.mockResolvedValue(snapshot)
})
afterEach(() => { cleanup(); vi.useRealTimers() })

it('S1-163 renders validation arrays as safe text without crashing Clone', async () => {
  cloneProject.mockRejectedValue({
    body: { detail: [
      { loc: ['body', 'name'], msg: 'Name is too long.' },
      { loc: ['body', 'name'], msg: 'Use at most 200 characters.' },
    ] },
  })
  render(<ProjectLifecyclePanel enabled projectId={project.project_id} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Clone project' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Name is too long. Use at most 200 characters.')
})

it('S1-163 humanizes each clone validation message before joining them', async () => {
  render(<CloneDialog open project={project} onClone={vi.fn().mockRejectedValue({
    body: { detail: [
      { msg: 'Name is too long.' },
      { msg: 'POST /api/projects -> 422' },
      { msg: { internal: 'detail' } },
    ] },
  })} />)
  fireEvent.click(screen.getByRole('button', { name: 'Clone project' }))
  expect((await screen.findByRole('alert')).textContent).toBe(
    'Name is too long. The service had a temporary problem. Try again. The clone did not go through. Nothing changed.',
  )
})

it('S1-163 keeps a long clone name within the server limit', async () => {
  getProjectLifecycle.mockResolvedValue({ ...snapshot, project: { ...project, name: 'a'.repeat(200) } })
  cloneProject.mockResolvedValue({ project, receipt: { receipt_id: 'copy-receipt' } })
  render(<ProjectLifecyclePanel enabled projectId={project.project_id} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Clone project' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
  await waitFor(() => expect(cloneProject).toHaveBeenCalledWith(project.project_id, `${'a'.repeat(193)} (copy)`))
})

it('S1-166 focuses confirmation and restores Reset after cancellation and completion', async () => {
  render(<DangerZone projectName="Roof" onReset={vi.fn().mockResolvedValue({ receipt: { receipt_id: 'reset-receipt' } })} />)
  fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
  expect(screen.getByRole('textbox')).toHaveFocus()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.getByRole('button', { name: 'Reset' })).toHaveFocus()
  fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Roof' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reset' })).toHaveFocus())
})

it('S1-167 moves and contains Clone focus and restores the trigger on Escape', () => {
  function Harness() {
    const [open, setOpen] = useState(false)
    return <><button onClick={() => setOpen(true)}>Open clone</button>
      <CloneDialog open={open} project={project} onClose={() => setOpen(false)} /></>
  }
  render(<Harness />)
  const trigger = screen.getByRole('button', { name: 'Open clone' })
  trigger.focus()
  fireEvent.click(trigger)
  const close = screen.getByRole('button', { name: 'Close' })
  expect(close).toHaveFocus()
  fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
  expect(screen.getByRole('button', { name: 'Clone project' })).toHaveFocus()
  fireEvent.keyDown(document.activeElement, { key: 'Tab' })
  expect(close).toHaveFocus()
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(trigger).toHaveFocus()
})

it('S1-167 makes Export a contained modal and dismisses it with Escape', () => {
  const onDismiss = vi.fn()
  const { container } = render(<ExportDialog onExport={vi.fn()} onDismiss={onDismiss} />)
  expect(container.querySelector('.export-dialog-backdrop')).toBeTruthy()
  expect(screen.getByRole('dialog')).toContainElement(document.activeElement)
  fireEvent.keyDown(document.activeElement, { key: 'Escape' })
  expect(onDismiss).toHaveBeenCalledOnce()
})

it('S1-163 humanizes offline export errors', async () => {
  render(<ExportDialog onExport={vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))} />)
  fireEvent.click(screen.getByRole('button', { name: 'Export' }))
  expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't reach the service.")
  expect(screen.getByRole('alert')).not.toHaveTextContent('Failed to fetch')
})

it('S1-170 shows indeterminate progress until measured progress exists', () => {
  render(<ExportDialog onExport={() => new Promise(() => {})} />)
  fireEvent.click(screen.getByRole('button', { name: 'Export' }))
  expect(screen.getByRole('progressbar')).not.toHaveAttribute('value')
  expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
  expect(screen.queryByText('0%')).toBeNull()
})

it('S1-170 reports timeout as an unknown outcome without inviting a duplicate export', async () => {
  vi.useFakeTimers()
  render(<ExportDialog onExport={() => new Promise(() => {})} timeoutMs={100} />)
  fireEvent.click(screen.getByRole('button', { name: 'Export' }))
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
  expect(screen.getByRole('alert')).toHaveTextContent('could not confirm whether the export finished')
  expect(screen.getByRole('alert')).not.toHaveTextContent('100 ms')
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
})

it('S1-172 preserves an open clone result during stale-data retry', async () => {
  getProjectLifecycle.mockResolvedValueOnce(snapshot).mockRejectedValueOnce(new TypeError('Failed to fetch'))
  cloneProject.mockResolvedValue({ project, receipt: { receipt_id: 'copy-receipt' } })
  render(<ProjectLifecyclePanel enabled projectId={project.project_id} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Clone project' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clone project' }))
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('copy-receipt')).toBeTruthy())
  let finish
  getProjectLifecycle.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(within(screen.getByRole('dialog')).getByText('copy-receipt')).toBeTruthy()
  await act(async () => { finish(snapshot) })
  expect(within(screen.getByRole('dialog')).getByText('copy-receipt')).toBeTruthy()
})
