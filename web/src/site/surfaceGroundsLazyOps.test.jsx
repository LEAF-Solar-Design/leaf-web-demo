import * as React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let SurfaceGrounds
let load
let resolveLoad
let rejectLoad

beforeEach(async () => {
  vi.resetModules()
  // Keep the renderer and the re-imported grounds on the same React instance.
  vi.doMock('react', () => React)
  const pending = new Promise((resolve, reject) => {
    resolveLoad = resolve
    rejectLoad = reject
  })
  load = vi.fn(() => pending)
  vi.doMock('../integrations/OpsWorkspaceGround.jsx', () => load())
  SurfaceGrounds = (await import('./SurfaceGrounds.jsx')).default
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.doUnmock('react')
  vi.doUnmock('../integrations/OpsWorkspaceGround.jsx')
})

function measuredWindow() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    top: 0, left: 0, right: 900, bottom: 700, width: 900, height: 700,
  })
}

it('never requests Ops before activation, including board occlusion and a leaving hint', async () => {
  const { container, rerender } = render(<SurfaceGrounds surface="cad" />)
  for (const props of [
    { surface: 'browser' }, { surface: 'ios' },
    { surface: 'ops', boardVisible: true },
    { surface: 'browser', leavingGround: 'integrations' },
  ]) {
    rerender(<SurfaceGrounds {...props} />)
    await act(async () => { await Promise.resolve() })
    expect(load).not.toHaveBeenCalled()
    expect(container.querySelector('[data-ground="ops"]')).toBeNull()
  }
})

it('loads once, hides pending on switches, and retains the loaded node with current authority props', async () => {
  measuredWindow()
  const mounts = vi.fn()
  const unmounts = vi.fn()
  const received = vi.fn()
  function LoadedOps(props) {
    React.useEffect(() => { mounts(); return unmounts }, [])
    received(props)
    return <section data-ground="ops" hidden={!props.active}>
      <input aria-label="Retained Ops state" defaultValue="original" />
    </section>
  }
  const firstSession = { generation: 1 }
  const first = { surface: 'ops', projectId: 'project-one', session: firstSession, signedIn: true, accountScope: 'one' }
  const { container, rerender } = render(<SurfaceGrounds {...first} studioShell />)
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  const pending = screen.getByText('Loading Ops workspace…').closest('[data-ground="ops"]')
  expect(pending).not.toHaveAttribute('hidden')
  expect(pending.querySelector('.ops-workspace')).toHaveAttribute('data-measured', 'true')
  rerender(<SurfaceGrounds {...first} surface="browser" studioShell />)
  expect(pending).toHaveAttribute('hidden')
  expect(container.querySelector('[data-ground="browser-asset"]')).not.toHaveAttribute('hidden')
  expect(screen.queryByRole('region', { name: 'Project workspace' })).toBeNull()
  rerender(<SurfaceGrounds {...first} boardVisible studioShell />)
  // The shared shell overlays Project instead of replacing the Ops ground.
  expect(pending).not.toHaveAttribute('hidden')
  fireEvent.click(screen.getByRole('button', { name: 'Project', exact: true }))
  expect(screen.getByRole('region', { name: 'Project sidebar' })).toBeVisible()
  expect(pending).not.toHaveAttribute('hidden')
  rerender(<SurfaceGrounds {...first} surface="ios" leavingGround="integrations" studioShell />)
  expect(pending).not.toHaveAttribute('hidden')
  expect(pending).toHaveAttribute('inert')
  expect(pending).toHaveAttribute('aria-hidden', 'true')
  expect(pending).toHaveAttribute('data-ground-phase', 'leaving')
  rerender(<SurfaceGrounds {...first} surface="browser" studioShell />)
  await act(async () => { resolveLoad({ OpsWorkspaceGround: LoadedOps }) })
  const input = await screen.findByLabelText('Retained Ops state')
  const ops = input.closest('[data-ground="ops"]')
  expect(ops).toHaveAttribute('hidden')
  input.value = 'retained'
  const nextSession = { generation: 2 }
  const next = { ...first, projectId: 'project-two', session: nextSession, signedIn: false, accountScope: 'two' }
  rerender(<SurfaceGrounds {...next} />)
  expect(ops).not.toHaveAttribute('hidden')
  expect(received.mock.lastCall[0]).toMatchObject({
    active: true, projectId: 'project-two', session: nextSession, signedIn: false, accountScope: 'two',
  })
  expect(received.mock.lastCall[0].session).toBe(nextSession)
  for (const props of [{ surface: 'browser' }, { surface: 'ops', boardVisible: true }, { surface: 'ops' }]) {
    rerender(<SurfaceGrounds {...next} {...props} />)
    expect(container.querySelector('[data-ground="ops"]')).toBe(ops)
    expect(ops.hidden).toBe(props.surface !== 'ops' || Boolean(props.boardVisible))
    expect(input.value).toBe('retained')
    expect(received.mock.lastCall[0].active).toBe(!ops.hidden)
  }
  expect(load).toHaveBeenCalledOnce()
  expect(mounts).toHaveBeenCalledOnce()
  expect(unmounts).not.toHaveBeenCalled()
})

it('contains a rejected load locally and applies ground visibility to its failure state', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const { container, rerender } = render(<SurfaceGrounds surface="ops" />)
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  const pendingWindow = screen.getByText('Loading Ops workspace…').closest('.ops-workspace')
  expect(pendingWindow).toHaveAttribute('data-measured', 'false')
  expect(pendingWindow.style.visibility).toBe('hidden')
  rerender(<SurfaceGrounds surface="browser" />)
  await act(async () => { rejectLoad(new Error('Chunk unavailable')) })
  const failure = await screen.findByText('Ops workspace could not be loaded. Reload the page to try again.')
  const ops = failure.closest('[data-ground="ops"]')
  expect(failure).toHaveAttribute('role', 'alert')
  expect(ops).toHaveAttribute('hidden')
  expect(container.querySelector('[data-ground="browser"]')).not.toHaveAttribute('hidden')
  rerender(<SurfaceGrounds surface="ios" leavingGround="integrations" />)
  expect(ops).not.toHaveAttribute('hidden')
  expect(ops).toHaveAttribute('inert')
  expect(ops).toHaveAttribute('aria-hidden', 'true')
  expect(container.querySelector('[data-ground="ios"]')).not.toHaveAttribute('hidden')
  rerender(<SurfaceGrounds surface="ops" />)
  expect(ops).not.toHaveAttribute('hidden')
  expect(ops).not.toHaveAttribute('inert')
  expect(ops).not.toHaveAttribute('aria-hidden')
  rerender(<SurfaceGrounds surface="ops" boardVisible />)
  expect(ops).toHaveAttribute('hidden')
  expect(container.querySelector('[data-ground="browser"]')).not.toHaveAttribute('hidden')
  expect(load).toHaveBeenCalledOnce()
})
