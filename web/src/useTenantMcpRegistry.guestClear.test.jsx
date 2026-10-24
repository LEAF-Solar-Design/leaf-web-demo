// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { listTenantMcpServers } from './api.js'
import useTenantMcpRegistry from './useTenantMcpRegistry.js'

vi.mock('./api.js', () => ({
  listTenantMcpServers: vi.fn().mockResolvedValue([{ id: 's1', label: 'One' }]),
  connectTenantMcpServer: vi.fn(),
  healthTenantMcpServer: vi.fn(),
  registerTenantMcpServer: vi.fn(),
  unlinkTenantMcpServer: vi.fn(),
}))

beforeEach(() => {
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
