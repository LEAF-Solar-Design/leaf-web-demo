// @vitest-environment jsdom
import React from 'react'
import { readFileSync } from 'node:fs'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { CockpitStatus, StatusToggles } from './DrawingCockpit.jsx'
import { focusFailureMessage } from './DrawingNavigationTools.jsx'
import LocalCadConnect from '../localCad/LocalCadConnect.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('S1-91 removes version keys and both Escape owners while the mounted stage is inactive', () => {
  // ToolCast owns the registrations; checking its wiring avoids booting its network-backed workspace.
  const source = readFileSync(`${process.cwd()}/src/site/ToolCast.jsx`, 'utf8')
  expect(source).toContain("useEscapeOwner('proposal', active && !!route,")
  expect(source).toContain("useEscapeOwner('detach', active && jobRunning,")
  expect(source).toMatch(/useEffect\(\(\) => \{\s*if \(!active\) return undefined\s*const onVersionKey/)
  expect(source).toContain('[active, drawing.versionBusy, onUndo, previewLocked, redo]')
})

it('S1-92 explains objects without bounds', () => {
  expect(focusFailureMessage('no bounds')).toBe('This object has no visible geometry to frame.')
})

it('S1-92 gives loading retry guidance', () => {
  expect(focusFailureMessage('viewer not ready')).toBe('The drawing is still loading. Wait a moment, then try again.')
})

it('S1-92 keeps unknown object and framing diagnostics off screen', () => {
  expect(focusFailureMessage('unknown id')).toBe('This object is no longer in the drawing. Search again.')
  expect(focusFailureMessage('frame refused')).toBe('The drawing could not zoom to this object. Try again.')
  expect(focusFailureMessage('GET /api/private -> 500')).not.toContain('/api/')
})

it('S1-99 disables unsupported fullscreen with a visible reason', () => {
  const original = Object.getOwnPropertyDescriptor(document.documentElement, 'requestFullscreen')
  Object.defineProperty(document.documentElement, 'requestFullscreen', { configurable: true, value: undefined })
  try {
    render(<StatusToggles />)
    expect(screen.getByRole('button', { name: 'Toggle fullscreen' }).disabled).toBe(true)
    expect(screen.getByText('Fullscreen is not available in this browser.')).toBeTruthy()
  } finally {
    if (original) Object.defineProperty(document.documentElement, 'requestFullscreen', original)
    else delete document.documentElement.requestFullscreen
  }
})

it('S1-99 announces rejected fullscreen requests without transport details', async () => {
  const request = vi.fn().mockRejectedValue(new Error('POST /api/private -> 500'))
  const original = Object.getOwnPropertyDescriptor(document.documentElement, 'requestFullscreen')
  Object.defineProperty(document.documentElement, 'requestFullscreen', { configurable: true, value: request })
  try {
    render(<StatusToggles />)
    fireEvent.click(screen.getByRole('button', { name: 'Toggle fullscreen' }))
    expect(await screen.findByText('Fullscreen could not be opened or closed. Try again.')).toBeTruthy()
    expect(document.body.textContent).not.toContain('/api/')
  } finally {
    if (original) Object.defineProperty(document.documentElement, 'requestFullscreen', original)
    else delete document.documentElement.requestFullscreen
  }
})

it('S1-105 labels expanded geometry as rendered paths', () => {
  render(<CockpitStatus ground={null} viewerRef={{ current: null }} shown={{ polylines: [1, 2, 3], layers: ['0'] }} />)
  expect(screen.getByTestId('cockpit-status').textContent).toContain('3 rendered paths')
  expect(screen.getByTestId('cockpit-status').textContent).not.toContain('3 entities')
})

it('S1-187 lets permission recovery wrap instead of clipping it', async () => {
  const permissions = { query: vi.fn().mockResolvedValue({ state: 'denied' }) }
  const original = Object.getOwnPropertyDescriptor(navigator, 'permissions')
  Object.defineProperty(navigator, 'permissions', { configurable: true, value: permissions })
  try {
    render(<LocalCadConnect status="unreachable" />)
    const message = await screen.findByText("Allow this site to reach apps on this computer in your browser's site settings")
    expect(message.closest('[aria-live]').style.whiteSpace).toBe('normal')
    expect(message.closest('[aria-live]').style.textOverflow).not.toBe('ellipsis')
  } finally {
    if (original) Object.defineProperty(navigator, 'permissions', original)
    else delete navigator.permissions
  }
})
