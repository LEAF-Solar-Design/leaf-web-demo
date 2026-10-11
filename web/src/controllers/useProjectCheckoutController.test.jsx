import React, { StrictMode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, act } from '@testing-library/react'
import { useProjectCheckoutController } from './useProjectCheckoutController.js'

afterEach(() => cleanup())

it('R7-35 StrictMode subscriptions clean up without acquiring releasing or disposing', async () => {
  const listeners = new Set()
  let snapshot = Object.freeze({ binding: null, checkout: null, ownsCheckout: false, pending: false, error: null })
  const client = {
    getSnapshot: () => snapshot,
    subscribe: vi.fn((listener) => { listeners.add(listener); return () => listeners.delete(listener) }),
    readCheckout: vi.fn(async () => ({ ok: true })), acquireCheckout: vi.fn(async () => ({ ok: true })),
    releaseCheckout: vi.fn(async () => ({ ok: true })), dispose: vi.fn(), setScope: vi.fn(),
  }
  const wrapper = ({ children }) => <StrictMode>{children}</StrictMode>
  const mounted = renderHook(() => useProjectCheckoutController(client), { wrapper })
  expect(listeners.size).toBe(1)
  expect(client.acquireCheckout).not.toHaveBeenCalled()
  act(() => { snapshot = Object.freeze({ ...snapshot, pending: true }); for (const listener of listeners) listener() })
  expect(mounted.result.current.state.pending).toBe(true)
  await act(async () => { await mounted.result.current.actions.refresh() })
  expect(client.readCheckout).toHaveBeenCalledOnce()
  mounted.unmount()
  expect(listeners.size).toBe(0)
  const remount = renderHook(() => useProjectCheckoutController(client), { wrapper })
  expect(listeners.size).toBe(1)
  remount.unmount()
  expect(listeners.size).toBe(0)
  expect(client.acquireCheckout).not.toHaveBeenCalled()
  expect(client.releaseCheckout).not.toHaveBeenCalled()
  expect(client.dispose).not.toHaveBeenCalled()
  expect(client.setScope).not.toHaveBeenCalled()
})
