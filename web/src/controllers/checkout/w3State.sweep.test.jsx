// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { useSyncExternalStore } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import CheckoutControls from '../../components/CheckoutControls.jsx'
import AuthorPanel from '../../components/AuthorPanel.jsx'
import NavRail from '../../site/NavRail.jsx'
import { createCheckoutController } from './createCheckoutController.js'
import { secureTakenCheckoutAuthority } from './useCheckoutController.js'
import useJobController, { PENDING_RUN_KEY, readPendingRun, savePendingRun } from '../useJobController.js'
import * as auth from '../../auth.js'

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

const services = () => ({ loadVersions: vi.fn().mockResolvedValue({ checkout: null }),
  take: vi.fn(), release: vi.fn() })
const controllerFor = (api) => createCheckoutController({ drawingId: 'drawing', holder: 'holder', services: api })

it('retains a humanized Take failure after refreshing the edit lock', async () => {
  const api = services()
  api.take.mockRejectedValue(new Error('POST /api/checkout -> 500'))
  const controller = controllerFor(api)
  await controller.take()
  expect(controller.getSnapshot().error).toBe('The service had a temporary problem. Try again.')
  expect(controller.getSnapshot().busy).toBe(false)
})

it('retains a Release failure after successful readback', async () => {
  const api = services()
  api.release.mockRejectedValue(new Error('Failed to fetch'))
  const controller = controllerFor(api)
  await controller.release()
  expect(controller.getSnapshot().error).toContain('Check the connection and retry.')
})

it('shows a failed Take and allows an explicit retry', async () => {
  const onTake = vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce({ acquired: true })
  render(<CheckoutControls onTake={onTake} />)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' })))
  expect(screen.getByRole('alert').textContent).toContain('could not be taken')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' })))
  expect(onTake).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('shows a failed Release without exposing transport text', async () => {
  render(<CheckoutControls heldByUs onRelease={vi.fn().mockRejectedValue(new Error('DELETE /api/checkout -> 502'))} />)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Release' })))
  expect(screen.getByRole('alert').textContent).toBe('The service had a temporary problem. Try again.')
})

it('explains a refused local lock even after the server grants Take', async () => {
  render(<CheckoutControls onTake={vi.fn().mockResolvedValue({ acquired: false })} />)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' })))
  expect(screen.getByRole('alert').textContent).toContain('Another tab or editor')
})

it('reports local authority refusal after releasing the unproven lease', async () => {
  const api = services()
  const controller = controllerFor(api)
  const stop = vi.fn()
  await secureTakenCheckoutAuthority({ controller, result: { acquired: true, checkout_capability: 'secret' },
    drawingId: 'drawing', holder: 'holder', services: api,
    holdAuthority: () => ({ active: false, acquired: Promise.resolve(false), stop }) })
  expect(api.release).toHaveBeenCalledWith('drawing', 'secret')
  expect(controller.getSnapshot().error).toContain('could not be secured in this tab')
  expect(controller.getCapability()).toBeNull()
})

it.each([false, true])('scopes a deferred local authority refusal to its starting drawing (switch: %s)', async (switchDrawing) => {
  const api = services()
  api.take.mockResolvedValue({ acquired: true, checkout_capability: 'secret' })
  const controller = controllerFor(api)
  let rejectAcquisition
  const acquired = new Promise((resolve, reject) => { rejectAcquisition = reject })
  const stop = vi.fn()
  const holdAuthority = vi.fn(() => ({ active: true, acquired, stop }))
  function Controls() {
    const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
    const take = async () => {
      const scopeGeneration = controller.getScopeGeneration()
      const result = await controller.takeDeferred()
      const authority = await secureTakenCheckoutAuthority({ controller, result,
        drawingId: 'drawing', holder: 'holder', services: api, holdAuthority, scopeGeneration })
      return { acquired: !!authority }
    }
    return <>
      <CheckoutControls {...state} drawingKey={state.drawingId} onTake={take} />
      {state.error && <span role="alert">{state.error}</span>}
    </>
  }
  await controller.refresh()
  render(<Controls />)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' })))
  expect(holdAuthority).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Take edit lock' }).disabled).toBe(true)
  if (switchDrawing) {
    await act(async () => {
      controller.setScope({ drawingId: 'drawing-b', holder: 'holder' })
      await controller.refresh()
    })
    expect(screen.getByRole('button', { name: 'Take edit lock' }).disabled).toBe(false)
  }
  await act(async () => rejectAcquisition(new Error('Local authority refused')))
  expect(api.release).toHaveBeenCalledWith('drawing', 'secret')
  expect(stop).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Take edit lock' }).disabled).toBe(false)
  if (switchDrawing) {
    expect(controller.getSnapshot().drawingId).toBe('drawing-b')
    expect(controller.getSnapshot().error).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  } else {
    expect(controller.getSnapshot().error).toContain('could not be secured in this tab')
    expect(screen.getByText(/could not be secured in this tab/)).toBeTruthy()
  }
})

it('keeps internal error codes and ids out of the edit lock read failure', () => {
  render(<CheckoutControls unknown readFailed failure={{ status: 503, errorCode: 'SECRET_CODE', errorId: 'private-id' }} onRetry={vi.fn()} />)
  expect(document.body.textContent).not.toMatch(/503|SECRET_CODE|private-id/)
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
})

it('hides another account’s pending inputs at mount and keeps them for its return', () => {
  const tokenA = `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'account-a' }))}.signature`
  const tokenB = `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'account-b' }))}.signature`
  localStorage.setItem('leaf.jwt', tokenA)
  const pending = savePendingRun('count', { private: 'roof' }, localStorage)
  const saved = localStorage.getItem(PENDING_RUN_KEY)
  localStorage.setItem('leaf.jwt', tokenB)
  expect(readPendingRun(localStorage)).toBeNull()
  const hook = renderHook(() => useJobController({ storage: localStorage, readsPaused: true }))
  expect(hook.result.current.pendingRun).toBeNull()
  expect(localStorage.getItem(PENDING_RUN_KEY)).toBe(saved)
  expect(JSON.parse(saved).account).toBe('studio:account-a')
  act(() => {
    localStorage.setItem('leaf.jwt', tokenA)
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt', newValue: tokenA }))
  })
  expect(hook.result.current.pendingRun).toEqual(pending)
  expect(localStorage.getItem(PENDING_RUN_KEY)).toBe(saved)
})

it('rejects legacy pending inputs with no account scope', () => {
  localStorage.setItem(PENDING_RUN_KEY, JSON.stringify({ v: 1, tool: 'count', params: { private: 'roof' }, ts: 100 }))
  expect(readPendingRun(localStorage, 101)).toBeNull()
})

it('records the account on saved pending inputs', () => {
  savePendingRun('count', { panels: 4 }, localStorage, 100, 'account-a')
  expect(JSON.parse(localStorage.getItem(PENDING_RUN_KEY)).account).toBe('account-a')
  expect(readPendingRun(localStorage, 101, 'account-a')).toEqual({ tool: 'count', params: { panels: 4 }, ts: 100 })
})

it('keeps account inputs private while sign-in is expired and restores them only for that account', () => {
  savePendingRun('count', { panels: 4 }, localStorage, 100, 'account-a')
  expect(readPendingRun(localStorage, 101, null)).toBeNull()
  expect(localStorage.getItem(PENDING_RUN_KEY)).not.toBeNull()
  expect(readPendingRun(localStorage, 102, 'account-a')?.params).toEqual({ panels: 4 })
})

it('clears saved pending inputs and their visible state before sign-out', () => {
  let logout
  vi.spyOn(auth, 'subscribeBeforeLogout').mockImplementation(callback => { logout = callback; return () => {} })
  savePendingRun('count', { panels: 4 }, localStorage)
  const hook = renderHook(() => useJobController({ storage: localStorage, readsPaused: true }))
  expect(hook.result.current.pendingRun).not.toBeNull()
  act(() => logout())
  expect(hook.result.current.pendingRun).toBeNull()
  expect(localStorage.getItem(PENDING_RUN_KEY)).toBeNull()
})

it('does not claim a plan upgrade will enable authoring when site availability is unknown', () => {
  render(<AuthorPanel buildEntitled={false} />)
  expect(screen.queryByText(/Upgrade to build/)).toBeNull()
  expect(screen.getByText(/Tool authoring is unavailable for this account or site/)).toBeTruthy()
})

it('explains Review & run being disabled by preview or an active run', () => {
  render(<NavRail activeSurface="browser" authorContent={null} running catalogFamilyCount={1}
    openFamilies={{ measurements: true }} railFamilies={[{ family_id: 'measurements', label: 'Measurements',
      capabilities: [{ name: 'count', description: 'Count panels', kind: 'read', params: {}, capabilities: [] }] }]} />)
  fireEvent.click(screen.getByRole('button', { name: /count/i }))
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
  expect(screen.getByText(/A run is in progress or an older version is open/)).toBeTruthy()
})

it('explains the edit lock on disabled editing tools', () => {
  render(<NavRail activeSurface="browser" authorContent={null} writeLocked writeEntitled catalogFamilyCount={1}
    openFamilies={{ editing: true }} railFamilies={[{ family_id: 'editing', label: 'Editing',
      capabilities: [{ name: 'place', description: 'Place panels', kind: 'write', params: {}, capabilities: ['drawing.write'] }] }]} />)
  fireEvent.click(screen.getByRole('button', { name: /place/i }))
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
  expect(screen.getByText(/Take the edit lock to run editing tools/)).toBeTruthy()
})
