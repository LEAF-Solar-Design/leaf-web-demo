import React, { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import LocalCadConnect from './LocalCadConnect.jsx'

afterEach(cleanup)

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
  for (const [status, label] of [['stopped', 'AutoCAD: not found on this computer'],
    ['connecting', 'AutoCAD: connecting'], ['unreachable', 'AutoCAD: not found on this computer'],
    ['waiting', 'AutoCAD: waiting for a drawing'], ['connected', 'Connected to AutoCAD · site.dwg']]) {
    rerender(<LocalCadConnect status={status} drawing="site.dwg" summary={summary} />)
    const line = screen.getByTestId('local-cad-status')
    expect(line.textContent).toBe(label)
    expect(line.getAttribute('aria-live')).toBe('polite')
    expect(screen.getByTestId('local-cad-summary').textContent).toBe(summary)
    expect(screen.getByTestId('local-cad-summary').querySelector('script')).toBeNull()
  }
})
