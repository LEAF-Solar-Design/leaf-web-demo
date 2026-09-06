// @vitest-environment jsdom
// Front-door slice of the Leaf workspace: render the real root, stage, and
// landing while stubbing the drawing and controller subtrees at their seams.
import { readFileSync } from 'node:fs'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  signedIn: vi.fn(),
  navigate: vi.fn(),
  demand: vi.fn(),
  assign: vi.fn(),
  replace: vi.fn(),
  callback: vi.fn(),
}))

vi.mock('../auth.js', () => ({
  isSignedIn: mocks.signedIn,
  isAuthRedirectCallback: () => false,
  handleRedirectCallback: mocks.callback,
}))
vi.mock('../api.js', () => ({ submitDemandCapture: mocks.demand }))
vi.mock('./intakeCache.js', () => ({ loadDemoSolve: () => new Promise(() => {}) }))
vi.mock('./router.js', () => ({
  useRoute: () => ({ path: window.location.pathname, hash: '' }),
  navigate: mocks.navigate,
}))
vi.mock('./StageLayer.jsx', async () => {
  const { forwardRef } = await import('react')
  return { default: forwardRef(() => null) }
})
vi.mock('./ToolCast.jsx', () => ({
  default: ({ active }) => <div data-testid="tool-stub" data-active={String(active)} />,
}))
vi.mock('./ContinuityStore.jsx', () => ({ default: ({ children }) => children }))
vi.mock('../App.jsx', () => ({ default: () => <div data-testid="app-stub" /> }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({
  WorkspaceControllerProvider: ({ children }) => children,
}))
vi.mock('../controllers/workspaceMount.js', () => ({
  operatorWorkspaceMount: () => ({}),
  consoleWorkspaceMount: () => ({}),
}))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({
  DRAWING_MODE_CONSOLE: 'console',
  DRAWING_MODE_OPERATOR: 'operator',
  DrawingIdentityProvider: ({ children, publicDemo }) => (
    <div data-testid="identity-stub" data-public-demo={String(publicDemo)}>{children}</div>
  ),
  useDrawingIdentity: () => ({ drawingId: null, setFromUpload: () => {} }),
}))

beforeEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
  mocks.signedIn.mockReturnValue(false)
  mocks.demand.mockResolvedValue({ ok: true })
  mocks.callback.mockResolvedValue(false)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function mountFrontDoor(path = '/', hostname = 'platform.leafdesign.ai') {
  const browserWindow = window
  const url = new URL(path, `https://${hostname}`)
  // jsdom's Location methods are non-configurable. Replace only the global
  // window's location view, keeping the real DOM and event methods intact.
  const location = {
    pathname: url.pathname,
    search: url.search,
    hostname: url.hostname,
    origin: url.origin,
    href: url.href,
    assign: mocks.assign,
    replace: mocks.replace,
  }
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location
      const value = Reflect.get(target, key, target)
      return typeof value === 'function' && /^[a-z]/.test(String(key)) ? value.bind(target) : value
    },
  }))
  const { default: SiteRoot } = await import('./SiteRoot.jsx')
  return render(<SiteRoot />)
}

describe('Leaf workspace guest front door', () => {
  it.each([
    'leaf-platform-web.vercel.app',
    'platform.leafdesign.ai',
    'platform-staging.leafdesign.ai',
  ])('keeps the signed-out root on %s and opens the local sandbox', async (hostname) => {
    await mountFrontDoor('/', hostname)
    expect(document.querySelector('.stage-root').getAttribute('data-scene')).toBe('site')
    expect(mocks.replace).not.toHaveBeenCalled()
    for (const name of ['Try Branch, no login required', 'Open workspace', 'Solve']) {
      fireEvent.click(screen.getByRole('button', { name }))
    }
    expect(mocks.assign.mock.calls).toEqual([
      ['/try?demo=1'], ['/try?demo=1'], ['/try?demo=1'],
    ])
    expect(mocks.navigate).not.toHaveBeenCalled()
    expect(mocks.demand).not.toHaveBeenCalled()
  })

  it('uses the same guest target for the T shortcut', async () => {
    await mountFrontDoor()
    fireEvent.keyDown(document.body, { key: 't' })
    expect(mocks.assign).toHaveBeenCalledWith('/try?demo=1')
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it('boots the guest target into the tool scene with public demo enabled', async () => {
    await mountFrontDoor('/try?demo=1')
    expect(document.querySelector('.stage-root').getAttribute('data-scene')).toBe('tool')
    expect(screen.getByTestId('identity-stub').getAttribute('data-public-demo')).toBe('true')
    expect(screen.getByTestId('tool-stub').getAttribute('data-active')).toBe('true')
    expect(screen.queryByTestId('app-stub')).toBeNull()
    expect(mocks.assign).not.toHaveBeenCalled()
    expect(mocks.replace).not.toHaveBeenCalled()
  })

  it('keeps signed-in workspace buttons and the shortcut on the existing /try navigation', async () => {
    mocks.signedIn.mockReturnValue(true)
    await mountFrontDoor()
    for (const name of ['Try Branch, no login required', 'Open workspace', 'Solve']) {
      fireEvent.click(screen.getByRole('button', { name }))
    }
    fireEvent.keyDown(document.body, { key: 'T' })
    expect(mocks.navigate.mock.calls).toEqual([['/try'], ['/try'], ['/try'], ['/try']])
    expect(mocks.assign).not.toHaveBeenCalled()
    expect(mocks.replace).not.toHaveBeenCalled()
  })

  it('shows a named pricing placeholder and routes pricing interest to demand capture', async () => {
    await mountFrontDoor()
    expect(document.querySelector('.lp-price').textContent).toBe('PRICING PLACEHOLDER')
    expect(screen.queryByText(/\$299|14-day trial|Start free trial/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '05 Pricing placeholder' }))
    const email = screen.getByLabelText('Interested in a bring-your-own-key workspace?')
    expect(document.activeElement).toBe(email)
    fireEvent.change(email, { target: { value: 'guest@example.com' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Register interest' }))
    await screen.findByText('Interest saved. No payment required.')
    expect(mocks.demand).toHaveBeenCalledWith({
      email: 'guest@example.com', interest: 'Bring-your-own-key workspace',
    })
    expect(mocks.assign).not.toHaveBeenCalled()
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it('keeps the sandbox available when demand capture fails', async () => {
    mocks.demand.mockRejectedValueOnce(new Error('unavailable'))
    await mountFrontDoor()
    fireEvent.change(screen.getByLabelText('Interested in a bring-your-own-key workspace?'), {
      target: { value: 'guest@example.com' },
    })
    fireEvent.submit(screen.getByRole('form', { name: 'Register interest' }))
    await screen.findByRole('alert')
    expect(screen.queryByText('Interest saved. No payment required.')).toBeNull()
    expect(screen.getByRole('button', { name: 'Register interest' }).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Try Branch, no login required' }))
    expect(mocks.assign).toHaveBeenCalledWith('/try?demo=1')
  })

  it('contains no handler assigning the visitor to leafautomation.ai or a payment funnel', () => {
    for (const file of ['./LandingCast.jsx', './StageScene.jsx', './SiteRoot.jsx']) {
      const source = readFileSync(new URL(file, import.meta.url), 'utf8')
      expect(source).not.toMatch(/window\.location\.assign\([^)]*leafautomation\.ai/)
      expect(source).not.toMatch(/get-started|startTrial|stripe/i)
    }
  })
})
