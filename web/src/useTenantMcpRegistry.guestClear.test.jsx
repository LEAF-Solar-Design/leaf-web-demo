// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { connectTenantMcpServer, listTenantMcpServers, registerTenantMcpServer, unlinkTenantMcpServer } from './api.js'
import useTenantMcpRegistry from './useTenantMcpRegistry.js'

vi.mock('./api.js', () => ({
  listTenantMcpServers: vi.fn().mockResolvedValue([{ id: 's1', label: 'One' }]),
  connectTenantMcpServer: vi.fn(),
  healthTenantMcpServer: vi.fn(),
  registerTenantMcpServer: vi.fn(),
  unlinkTenantMcpServer: vi.fn(),
}))

beforeEach(() => {
  listTenantMcpServers.mockClear()
  listTenantMcpServers.mockResolvedValue([{ id: 's1', label: 'One' }])
})

afterEach(cleanup)

it('clears signed-in servers when mock becomes true', async () => {
  const { result, rerender } = renderHook(
    ({ mock }) => useTenantMcpRegistry({ mock }),
    { initialProps: { mock: false } },
  )
  await waitFor(() => expect(result.current.servers.length).toBe(1))

  rerender({ mock: true })

  expect(result.current.servers).toEqual([])
})

it('a registration that settles after the switch to guest does not re-list signed-in servers', async () => {
  let settle
  const pending = new Promise((resolve, reject) => { settle = { resolve, reject } })
  registerTenantMcpServer.mockImplementation(() => pending)
  const { result, rerender } = renderHook(
    ({ mock }) => useTenantMcpRegistry({ mock }),
    { initialProps: { mock: false } },
  )
  await waitFor(() => expect(result.current.servers.length).toBe(1))

  let call
  act(() => { call = result.current.register('https://mcp.example', 'Example') })
  rerender({ mock: true })
  settle.resolve({ id: 's2' })
  await act(async () => { await call })

  expect(listTenantMcpServers).toHaveBeenCalledTimes(1)
  expect(result.current.servers).toEqual([])
})

it('a connect or unlink that settles after the switch to guest does not re-list signed-in servers', async () => {
  {
    let settle
    const pending = new Promise((resolve, reject) => { settle = { resolve, reject } })
    connectTenantMcpServer.mockImplementation(() => pending)
    const { result, rerender } = renderHook(
      ({ mock }) => useTenantMcpRegistry({ mock }),
      { initialProps: { mock: false } },
    )
    await waitFor(() => expect(result.current.servers.length).toBe(1))

    let call
    act(() => { call = result.current.connect('s1') })
    rerender({ mock: true })
    settle.resolve({ id: 's1' })
    await act(async () => { await call })

    expect(listTenantMcpServers).toHaveBeenCalledTimes(1)
    expect(result.current.servers).toEqual([])
  }

  cleanup()
  listTenantMcpServers.mockClear()

  {
    let settle
    const pending = new Promise((resolve, reject) => { settle = { resolve, reject } })
    unlinkTenantMcpServer.mockImplementation(() => pending)
    const { result, rerender } = renderHook(
      ({ mock }) => useTenantMcpRegistry({ mock }),
      { initialProps: { mock: false } },
    )
    await waitFor(() => expect(result.current.servers.length).toBe(1))

    let call
    act(() => { call = result.current.unlink('s1') })
    rerender({ mock: true })
    settle.resolve({ id: 's1' })
    await act(async () => { await call })

    expect(listTenantMcpServers).toHaveBeenCalledTimes(1)
    expect(result.current.servers).toEqual([])
  }
})

it('a registration that fails after the switch to guest leaves no error', async () => {
  let settle
  const pending = new Promise((resolve, reject) => { settle = { resolve, reject } })
  registerTenantMcpServer.mockImplementation(() => pending)
  const { result, rerender } = renderHook(
    ({ mock }) => useTenantMcpRegistry({ mock }),
    { initialProps: { mock: false } },
  )
  await waitFor(() => expect(result.current.servers.length).toBe(1))

  let call
  act(() => { call = result.current.register('https://mcp.example', 'Example').catch(() => {}) })
  rerender({ mock: true })
  settle.reject(new Error('signed-in failure'))
  await act(async () => { await call })

  expect(result.current.error).toBe(null)
})
