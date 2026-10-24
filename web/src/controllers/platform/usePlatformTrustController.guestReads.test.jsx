import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import usePlatformTrustController from './usePlatformTrustController.js'

afterEach(cleanup)

function fakeServices() {
  return {
    getUsage: vi.fn().mockResolvedValue({ used: 0 }),
    getEntitlements: vi.fn().mockResolvedValue({ features: {} }),
    getHealth: vi.fn().mockResolvedValue({ ok: true }),
    getClaudeGrant: vi.fn().mockResolvedValue({ linked: false }),
    activateClaudeGrant: vi.fn().mockResolvedValue({ linked: true }),
    linkClaudeGrant: vi.fn().mockResolvedValue({ linked: true }),
    unlinkClaudeGrant: vi.fn().mockResolvedValue({ linked: false }),
  }
}

describe('platform trust guest reads', () => {
  it('clears signed-in usage, entitlements and grant when reads pause', async () => {
    const services = fakeServices()
    const { result, rerender } = renderHook(
      ({ guestReadsPaused }) => usePlatformTrustController({ mock: false, guestReadsPaused, services }),
      { initialProps: { guestReadsPaused: false } },
    )
    await waitFor(() => {
      expect(result.current.usage).toEqual({ used: 0 })
      expect(result.current.entitlements).toEqual({ features: {} })
      expect(result.current.grant).toEqual({ linked: false })
    })

    rerender({ guestReadsPaused: true })

    expect(result.current.usage).toBeNull()
    expect(result.current.entitlements).toBeNull()
    expect(result.current.grant).toBeNull()
  })

  it('loads only public health for a known guest', async () => {
    const services = fakeServices()
    const { result } = renderHook(() => usePlatformTrustController({
      mock: false, guestReadsPaused: true, services,
    }))

    await waitFor(() => expect(result.current.health).toEqual({ ok: true }))
    expect(services.getHealth).toHaveBeenCalledTimes(1)
    for (const [name, service] of Object.entries(services)) {
      if (name !== 'getHealth') expect(service).not.toHaveBeenCalled()
    }
  })

  it('refreshes all four reads once when the guest pause clears', async () => {
    const services = fakeServices()
    const { result, rerender } = renderHook(
      ({ guestReadsPaused }) => usePlatformTrustController({ mock: false, guestReadsPaused, services }),
      { initialProps: { guestReadsPaused: true } },
    )
    await waitFor(() => expect(result.current.health).toEqual({ ok: true }))

    rerender({ guestReadsPaused: false })

    await waitFor(() => expect(result.current.grant).toEqual({ linked: false }))
    expect(services.getUsage).toHaveBeenCalledTimes(1)
    expect(services.getEntitlements).toHaveBeenCalledTimes(1)
    expect(services.getClaudeGrant).toHaveBeenCalledTimes(1)
    expect(services.getHealth).toHaveBeenCalledTimes(2)
  })

  it('calls no service in mock mode even with the guest pause set', async () => {
    const services = fakeServices()
    await act(async () => {
      renderHook(() => usePlatformTrustController({ mock: true, guestReadsPaused: true, services }))
    })

    for (const service of Object.values(services)) expect(service).not.toHaveBeenCalled()
  })

  it('retains all four initial reads by default', async () => {
    const services = fakeServices()
    const { result } = renderHook(() => usePlatformTrustController({ mock: false, services }))

    await waitFor(() => expect(result.current.grant).toEqual({ linked: false }))
    expect(services.getUsage).toHaveBeenCalledTimes(1)
    expect(services.getEntitlements).toHaveBeenCalledTimes(1)
    expect(services.getHealth).toHaveBeenCalledTimes(1)
    expect(services.getClaudeGrant).toHaveBeenCalledTimes(1)
  })
})
