// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import PromptBox, { shouldSkipPromptFocus } from './components/PromptBox.jsx'
import OperatorEntry from './components/operator/OperatorEntry.jsx'
import { getDrawingVersions, listOperatorSessions, searchIndex } from './api.js'
import { resetOperatorProbeForTests } from './operatorClient.js'
import * as operatorClient from './operatorClient.js'
import { isSignedIn } from './auth.js'
import { consoleKnownGuest, consoleSignedOut } from './controllers/session/consoleGate.js'
import { trackUsage } from './telemetry.js'

vi.mock('./api.js', async (importOriginal) => ({
  ...await importOriginal(),
  getDrawingVersions: vi.fn(),
  listOperatorSessions: vi.fn(),
  searchIndex: vi.fn(),
}))
vi.mock('./telemetry.js', async (importOriginal) => ({
  ...await importOriginal(),
  trackUsage: vi.fn(),
}))

const noop = () => {}
function mount(props = {}) {
  return render(<PromptBox value="panel" onChange={noop} onDispatch={noop}
    mcpDiscoveryEnabled={false} drawingId="demo" {...props} />)
}
function openScope(container, scope) {
  fireEvent.click(container.querySelector('.bar-scope'))
  fireEvent.click(screen.getByText(scope))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  resetOperatorProbeForTests()
  getDrawingVersions.mockResolvedValue({ versions: [{ v: 2, note: 'Demo panels' }] })
  listOperatorSessions.mockResolvedValue({ sessions: [] })
  searchIndex.mockResolvedValue({ results: [] })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 404, ok: false }))
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('guest command scopes', () => {
  it('does not search or emit usage without explicit live access', async () => {
    vi.useFakeTimers()
    const { container } = mount()
    openScope(container, 'find')
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(screen.getByText('Sign in to search your projects.')).toBeTruthy()
    expect(searchIndex).not.toHaveBeenCalled()
    expect(trackUsage).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('does not probe act indexes or emit usage when a guest picks a local action', () => {
    const onSelect = vi.fn()
    const { container } = mount({ paletteActions: [{ id: 'fit', label: 'Fit panels', onSelect }] })
    openScope(container, 'act')
    fireEvent.click(screen.getByText('Fit panels'))
    expect(onSelect).toHaveBeenCalledOnce()
    expect(getDrawingVersions).not.toHaveBeenCalled()
    expect(listOperatorSessions).not.toHaveBeenCalled()
    expect(trackUsage).not.toHaveBeenCalled()
  })

  it('uses the demo version store in act without live requests or usage', async () => {
    const { container } = mount({ value: '', mock: true })
    openScope(container, 'act')
    await screen.findByText('v2')
    expect(getDrawingVersions).toHaveBeenCalledWith(true, 'demo')
    expect(listOperatorSessions).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('v2'))
    expect(trackUsage).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('ignores live permission in demo find and does not send search usage', async () => {
    vi.useFakeTimers()
    const { container } = mount({ mock: true, liveCallsAllowed: true })
    openScope(container, 'find')
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(searchIndex).not.toHaveBeenCalled()
    expect(trackUsage).not.toHaveBeenCalled()
  })

  it('cancels a pending search when live permission is removed', async () => {
    vi.useFakeTimers()
    const view = mount({ liveCallsAllowed: true })
    openScope(view.container, 'find')
    view.rerender(<PromptBox value="panel" onChange={noop} onDispatch={noop}
      mcpDiscoveryEnabled={false} drawingId="demo" liveCallsAllowed={false} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(searchIndex).not.toHaveBeenCalled()
    expect(trackUsage).not.toHaveBeenCalled()
  })
})

describe('operator mount gate', () => {
  // Exercise App's production guard without mounting its WebGL controllers.
  const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
  const guard = source.match(/\{([^{}\n]+) && <OperatorEntry \/>\}/)[1]
  const allowsOperator = new Function('mock', 'knownGuest', 'signedIn', 'signedOut', `return ${guard}`)

  for (const authRequired of [false, true]) {
    it(`never probes without a token when auth is unconfigured and authRequired is ${authRequired}`, async () => {
      const probe = vi.spyOn(operatorClient, 'probeOperatorConsole')
      const session = { mock: false, authConfigured: false, authRequired, isSignedIn, authCallback: false }
      const signedIn = isSignedIn()
      const knownGuest = consoleKnownGuest(session)
      const signedOut = consoleSignedOut(session)
      expect(signedIn).toBe(false)
      expect(knownGuest).toBe(false)
      expect(signedOut).toBe(authRequired)
      const { container } = render(<>{allowsOperator(false, knownGuest, signedIn, signedOut) && <OperatorEntry />}</>)
      await act(async () => { await Promise.resolve() })
      expect(container.firstChild).toBeNull()
      expect(probe).not.toHaveBeenCalled()
      expect(fetch).not.toHaveBeenCalled()
    })
  }

  it('mounts and probes for a signed-in visitor in live mode', async () => {
    const probe = vi.spyOn(operatorClient, 'probeOperatorConsole').mockResolvedValue(true)
    render(<>{allowsOperator(false, false, true, false) && <OperatorEntry />}</>)
    expect(await screen.findByRole('button', { name: 'Open operator console' })).toBeTruthy()
    expect(probe).toHaveBeenCalledOnce()
  })

  it('keeps the operator unmounted in demo mode even when signed in', async () => {
    const probe = vi.spyOn(operatorClient, 'probeOperatorConsole')
    const { container } = render(<>{allowsOperator(true, false, true, false) && <OperatorEntry />}</>)
    await act(async () => { await Promise.resolve() })
    expect(container.firstChild).toBeNull()
    expect(probe).not.toHaveBeenCalled()
  })
})

describe('operator denial', () => {
  for (const status of [401, 403, 404]) {
    it(`renders and logs nothing for a ${status} probe`, async () => {
      fetch.mockResolvedValue({ status, ok: false })
      const error = vi.spyOn(console, 'error').mockImplementation(noop)
      const warn = vi.spyOn(console, 'warn').mockImplementation(noop)
      const { container } = render(<OperatorEntry />)
      await act(async () => { await Promise.resolve() })
      await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
      expect(container.firstChild).toBeNull()
      expect(error).not.toHaveBeenCalled()
      expect(warn).not.toHaveBeenCalled()
    })
  }
})

describe('prompt focus guard', () => {
  it('skips text inside dialogs and modal panels', () => {
    for (const attributes of ['role="dialog"', 'role="alertdialog"', 'aria-modal="true"']) {
      const host = document.createElement('div')
      host.innerHTML = `<section ${attributes}><p>Tour instructions</p></section>`
      expect(shouldSkipPromptFocus(host.querySelector('p'))).toBe(true)
    }
  })

  it('skips custom controls, focusable elements and editable descendants', () => {
    for (const attributes of ['role="tab"', 'role="menuitem"', 'role="switch"', 'role="checkbox"',
      'role="radio"', 'tabindex="0"', 'tabindex="-1"', 'contenteditable=""', 'contenteditable="plaintext-only"']) {
      const host = document.createElement('div')
      host.innerHTML = `<div ${attributes}><span>Control text</span></div>`
      expect(shouldSkipPromptFocus(host.querySelector('span'))).toBe(true)
    }
  })

  it('skips coarse pointers while allowing blank desktop surface clicks', () => {
    const target = document.createElement('div')
    expect(shouldSkipPromptFocus(target)).toBe(false)
    matchMedia.mockReturnValue({ matches: true })
    expect(shouldSkipPromptFocus(target)).toBe(true)
    expect(matchMedia).toHaveBeenCalledWith('(pointer: coarse)')
  })
})
