// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LandingCast from './LandingCast.jsx'
import SiteRoot from './SiteRoot.jsx'
import SheetsPage from './sheets/SheetsPage.jsx'

const mocks = vi.hoisted(() => ({ solve: vi.fn(), navigate: vi.fn(), replace: vi.fn() }))
vi.mock('./intakeCache.js', () => ({ loadDemoSolve: mocks.solve }))
vi.mock('./router.js', () => ({
  navigate: mocks.navigate,
  useRoute: () => ({ path: window.location.pathname, hash: window.location.hash }),
}))
vi.mock('../api.js', () => ({ submitDemandCapture: vi.fn() }))
vi.mock('../auth.js', () => ({
  isSignedIn: () => false,
  isAuthRedirectCallback: () => false,
  handleRedirectCallback: vi.fn(),
}))
vi.mock('./StageScene.jsx', () => ({ default: () => null }))
vi.mock('./ContinuityStore.jsx', () => ({ default: ({ children }) => children }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({
  WorkspaceControllerProvider: ({ children }) => children,
}))
vi.mock('../controllers/workspaceMount.js', () => ({ consoleWorkspaceMount: () => ({}) }))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({
  DRAWING_MODE_CONSOLE: 'console',
  DRAWING_MODE_OPERATOR: 'operator',
  DrawingIdentityProvider: ({ children }) => children,
}))
vi.mock('../components/SessionGate.jsx', () => ({ DemandCaptureCard: () => null }))

function setLocation(path, hostname = 'www.leafautomation.ai') {
  const browserWindow = window
  const url = new URL(path, `https://${hostname}`)
  const location = {
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    hostname: url.hostname,
    replace: mocks.replace,
  }
  // Follow frontDoor.test.jsx: jsdom's native Location cannot be replaced
  // directly, so retain the DOM and event methods through a window proxy.
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location
      const value = Reflect.get(target, key, target)
      return typeof value === 'function' && /^[a-z]/.test(String(key)) ? value.bind(target) : value
    },
  }))
  return location
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.solve.mockReturnValue(new Promise(() => {}))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('landing issue sweep', () => {
  it('shows the fixed module as plain text without dropdown styling or a caret (S1-93)', () => {
    const { container } = render(<LandingCast />)
    const module = screen.getByText('Q.Peak 425')
    expect(module.tagName).toBe('SPAN')
    expect(module.hasAttribute('tabindex')).toBe(false)
    expect(module.hasAttribute('role')).toBe(false)
    expect(container.querySelector('.lp-solver-field, .lp-solver-caret')).toBeNull()
    expect(screen.queryByText('▾')).toBeNull()
  })

  it('replaces failed progress with safe copy and retries the solve (S1-93)', async () => {
    let resolveRetry
    mocks.solve.mockRejectedValueOnce(new Error('GET /api/site/demo-solve -> 503'))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve }))
    render(<LandingCast />)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the string length. Please retry.')
    expect(screen.queryByText('Solving')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.solve).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Solving')).toBeTruthy()
    resolveRetry({ electrical: { max_modules_per_string: 28 } })
    expect(await screen.findByText('28')).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('offers retry when a response contains no string length (S1-93)', async () => {
    mocks.solve.mockResolvedValue({ ok: false })
    render(<LandingCast />)
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
  })

  it('offers another retry if the retry also fails (S1-93)', async () => {
    mocks.solve.mockRejectedValue(new Error('Failed to fetch'))
    render(<LandingCast />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.solve).toHaveBeenCalledTimes(2))
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the string length. Please retry.')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
    expect(screen.queryByText(/Failed to fetch/)).toBeNull()
  })

  it.each([
    ['an unsuccessful response', { ok: false, electrical: { max_modules_per_string: 28 } }],
    ['a missing string length', { ok: true, electrical: {} }],
  ])('refetches after %s and renders a good retry (S1-93)', async (_label, firstResponse) => {
    vi.resetModules()
    vi.stubEnv('VITE_MOCK', '1')
    vi.stubEnv('VITE_TELEMETRY_DISABLED', '1')
    const { loadDemoSolve } = await vi.importActual('./intakeCache.js')
    const fetchSolve = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => firstResponse })
      .mockResolvedValueOnce({ ok: true, json: async () => ({
        ok: true, solve: { electrical: { max_modules_per_string: 30 } },
      }) })
    vi.stubGlobal('fetch', fetchSolve)
    mocks.solve.mockImplementation(loadDemoSolve)
    render(<LandingCast />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('30')).toBeTruthy()
    expect(mocks.solve).toHaveBeenCalledTimes(2)
    expect(fetchSolve.mock.calls).toEqual([['/demo-solve.json'], ['/demo-solve.json']])
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('Solving')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('counts the cover and five sheets while preserving the pricing destination (S1-96)', () => {
    const { container } = render(<LandingCast />)
    const tabs = container.querySelectorAll('.lp-index .lp-tab')
    expect(tabs.length).toBe(6)
    expect(screen.getByText('Sheet 01 of 06, Cover')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '05 Pricing' }))
    expect(mocks.navigate).toHaveBeenCalledWith('/sheets#c-201')
  })

  it.each([
    ['02 The problem', 'a-101'],
    ['03 How Branch works', 'a-102'],
    ['04 Proof', 'e-401'],
    ['05 Pricing', 'c-201'],
    ['06 Docs', 's-501'],
  ])('selects the actual destination sheet for %s (S1-96)', (label, target) => {
    const location = setLocation('/')
    mocks.navigate.mockImplementation((destination) => {
      const url = new URL(destination, 'https://www.leafautomation.ai')
      location.pathname = url.pathname
      location.hash = url.hash
    })
    const frame = vi.fn()
    vi.stubGlobal('requestAnimationFrame', frame)
    const landing = render(<LandingCast />)
    fireEvent.click(screen.getByRole('button', { name: label }))
    expect(mocks.navigate).toHaveBeenCalledWith(`/sheets#${target}`)
    landing.unmount()
    render(<SheetsPage />)
    const sheet = document.getElementById(target)
    expect(sheet).toBeTruthy()
    sheet.scrollIntoView = vi.fn()
    expect(frame).toHaveBeenCalledTimes(1)
    frame.mock.calls[0][0]()
    expect(sheet.scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' })
  })

  it.each([
    ['leaf-platform-web.vercel.app', '/sheets#a-101', 'a-101'],
    ['app.leafdesign.ai', '/sheets#a-102', 'a-102'],
    ['platform.leafdesign.ai', '/sheets#e-401', 'e-401'],
    ['platform-staging.leafdesign.ai', '/sheets#c-201', 'c-201'],
    ['platform.leafdesign.ai', '/sheets#s-501', 's-501'],
    ['platform.leafdesign.ai', '/sheets/C-201/#a-101', 'c-201'],
    ['platform.leafdesign.ai', '/sheets/z-999#S-501', 's-501'],
  ])('preserves the selected sheet from %s%s in the marketing redirect', (hostname, path, target) => {
    setLocation(path, hostname)
    render(<SiteRoot />)
    expect(mocks.replace).toHaveBeenCalledWith(`https://www.leafautomation.ai/sheets#${target}`)
  })

  it.each(['/sheets', '/sheets#05', '/sheets#z-999', '/sheets/z-999'])('keeps unrecognized targets at the marketing hub: %s', (path) => {
    setLocation(path, 'platform.leafdesign.ai')
    render(<SiteRoot />)
    expect(mocks.replace).toHaveBeenCalledWith('https://www.leafautomation.ai/sheets')
  })

  it('keeps sheets on the marketing host without redirecting', () => {
    setLocation('/sheets#c-201')
    render(<SiteRoot />)
    expect(mocks.replace).not.toHaveBeenCalled()
  })

  it('uses the ratified product name in the top bar (S1-211)', () => {
    const { container } = render(<LandingCast />)
    expect(container.querySelector('.lp-brandsub').textContent).toBe('/ Leaf Automation Studio')
    expect(container.textContent).not.toMatch(/Leaf Build|utility-estimation/)
  })
})
