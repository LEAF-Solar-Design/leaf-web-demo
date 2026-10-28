import * as React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'

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
  vi.doMock('../lab/LabGround.jsx', () => load())
  SurfaceGrounds = (await import('./SurfaceGrounds.jsx')).default
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.doUnmock('react')
  vi.doUnmock('../lab/LabGround.jsx')
})

function measuredWindow() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    top: 0, left: 0, right: 900, bottom: 700, width: 900, height: 700,
  })
}

it('shows a Lab status in its themed ground while the import is pending', async () => {
  measuredWindow()
  const { container, rerender } = render(<SurfaceGrounds surface="lab" studioShell />)
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  const lab = container.querySelector('section[data-ground="lab"][data-leaf-theme="dark"]')
  expect(lab).toHaveAttribute('data-leaf-profile', 'studio')
  expect(lab).toHaveAttribute('data-studio-shell', 'cockpit')
  expect(lab).toHaveAttribute('aria-label', 'Lab workspace')
  expect(lab).not.toHaveAttribute('hidden')
  expect(lab.querySelector('[role="status"]')).toHaveTextContent('Loading Lab…')
  expect(lab.querySelector('[data-measured]')).toHaveAttribute('data-measured', 'true')
  expect(screen.getByRole('status')).toBeVisible()
  rerender(<SurfaceGrounds surface="browser" studioShell />)
  expect(lab).toHaveAttribute('hidden')
  expect(load).toHaveBeenCalledOnce()
})

it('contains a rejected import and shows the reload message with current ground visibility', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const { rerender } = render(<SurfaceGrounds surface="lab" />)
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  const pendingWindow = screen.getByText('Loading Lab…').closest('[data-measured]')
  expect(pendingWindow).toHaveAttribute('data-measured', 'false')
  expect(pendingWindow.style.visibility).toBe('hidden')
  await act(async () => { rejectLoad(new Error('Chunk unavailable')) })
  const failure = await screen.findByText('Lab could not be loaded. Reload the page to try again.')
  expect(failure).toHaveAttribute('role', 'alert')
  const lab = failure.closest('[data-ground="lab"]')
  expect(lab).not.toHaveAttribute('hidden')
  rerender(<SurfaceGrounds surface="browser" />)
  expect(lab).toHaveAttribute('hidden')
  measuredWindow()
  rerender(<SurfaceGrounds surface="lab" studioShell />)
  expect(screen.getByRole('alert')).toBeVisible()
  expect(load).toHaveBeenCalledOnce()
})

it('replaces the loading status with the resolved Lab ground', async () => {
  measuredWindow()
  render(<SurfaceGrounds surface="lab" studioShell />)
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  expect(screen.getByRole('status')).toHaveTextContent('Loading Lab…')
  function LoadedLab({ active }) {
    return <section data-ground="lab" hidden={!active}>Loaded Lab workspace</section>
  }
  await act(async () => { resolveLoad({ default: LoadedLab }) })
  expect(await screen.findByText('Loaded Lab workspace')).toBeVisible()
  expect(screen.queryByText('Loading Lab…')).toBeNull()
  expect(load).toHaveBeenCalledOnce()
})

it('never requests or renders Lab before activation', async () => {
  const { container, rerender } = render(<SurfaceGrounds surface="cad" />)
  for (const props of [
    { surface: 'browser' }, { surface: 'ios' },
    { surface: 'lab', boardVisible: true },
    { surface: 'browser', leavingGround: 'lab' },
  ]) {
    rerender(<SurfaceGrounds {...props} />)
    await act(async () => { await Promise.resolve() })
    expect(load).not.toHaveBeenCalled()
    expect(container.querySelector('[data-ground="lab"]')).toBeNull()
    expect(screen.queryByText('Loading Lab…')).toBeNull()
  }
})
