import React, { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import LocalCadConnect from './LocalCadConnect.jsx'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('toggles connect and disconnect through the owner callbacks', () => {
  const connected = vi.fn(), disconnected = vi.fn()
  function Owner() {
    const [status, setStatus] = useState('stopped')
    return <LocalCadConnect status={status}
      onConnect={() => { connected(); setStatus('connecting') }}
      onDisconnect={() => { disconnected(); setStatus('stopped') }} />
  }
  render(<Owner />)
  fireEvent.click(screen.getByTestId('local-cad-connect'))
  expect(connected).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('local-cad-connect').textContent).toBe('Disconnect')
  fireEvent.click(screen.getByTestId('local-cad-connect'))
  expect(disconnected).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('local-cad-connect').textContent).toBe('Connect AutoCAD')
})

it('announces every status in plain text and preserves the summary verbatim', () => {
  const summary = 'LEAFGENERATE added 108 objects <script>alert(1)</script>'
  const { rerender } = render(<LocalCadConnect />)
  for (const [status, label] of [['stopped', 'AutoCAD: not connected'],
    ['connecting', 'AutoCAD: connecting'], ['unreachable', 'AutoCAD: not found on this computer'],
    ['waiting', 'AutoCAD: waiting for a drawing'], ['connected', 'Connected to AutoCAD · site.dwg']]) {
    rerender(<LocalCadConnect status={status} drawing="site.dwg" summary={summary} />)
    const line = screen.getByTestId('local-cad-status')
    expect(line.textContent).toBe(label)
    expect(line.closest('[aria-live]').getAttribute('aria-live')).toBe('polite')
    expect(line.closest('[aria-live]').contains(screen.getByTestId('local-cad-summary'))).toBe(true)
    expect(screen.getByTestId('local-cad-summary').textContent).toBe(summary)
    expect(screen.getByTestId('local-cad-summary').querySelector('script')).toBeNull()
  }
})

it('keeps Disconnect available while unreachable and announces truncation', () => {
  const onDisconnect = vi.fn()
  render(<LocalCadConnect status="unreachable" summary="Added 2 objects" truncated onDisconnect={onDisconnect} />)
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(onDisconnect).toHaveBeenCalledOnce()
  expect(screen.getByTestId('local-cad-summary').textContent).toBe('Added 2 objects (showing the first 200,000 points)')
})

it('explains denied loopback access and tolerates unsupported permission queries', async () => {
  const query = vi.fn().mockResolvedValue({ state: 'denied' })
  vi.stubGlobal('navigator', { permissions: { query } })
  const view = render(<LocalCadConnect status="unreachable" />)
  await waitFor(() => expect(screen.getByTestId('local-cad-status').textContent)
    .toBe("Allow this site to reach apps on this computer in your browser's site settings"))
  expect(query).toHaveBeenCalledWith({ name: 'loopback-network' })
  query.mockRejectedValue(new Error('unsupported'))
  view.unmount()
  render(<LocalCadConnect status="unreachable" />)
  await waitFor(() => expect(screen.getByTestId('local-cad-status').textContent).toBe('AutoCAD: not found on this computer'))
})
