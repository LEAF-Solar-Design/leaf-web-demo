// @vitest-environment jsdom
//
// The ONE-SHELL render receipt (W7, docs/convergence/ACCEPTANCE.md Version 3):
// a real jsdom mount of SiteRoot proving scene 'app' renders the studio shell
// whatever a legacy `__LEAF_FLAGS` global says. The old shell and its rail are
// deleted, so an absent global, '0' and '1' must all produce the same studio
// DOM; a leftover read of the global that picked a different arm fails here.
//
// The heavy subtrees are stubbed at their module seams (three.js cannot run
// under jsdom); the REAL module under test is SiteRoot's scene logic. Every
// mount resets the module registry and sets the global BEFORE the dynamic
// import, so a module-eval read of it could not hide behind a cached module.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('./StageScene.jsx', () => ({
  default: ({ scene }) => <main className="stage-stub" data-testid="stage-stub" data-scene={scene} />,
}))
vi.mock('../App.jsx', () => ({
  default: () => <div data-testid="app-stub">console</div>,
}))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({
  WorkspaceControllerProvider: ({ children }) => <div data-testid="wcp-stub">{children}</div>,
  useWorkspaceControllers: () => { throw new Error('not mounted in this test') },
}))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({
  DRAWING_MODE_CONSOLE: 'console',
  DRAWING_MODE_OPERATOR: 'operator',
  DrawingIdentityProvider: ({ children }) => <div data-testid="identity-stub">{children}</div>,
}))
vi.mock('../auth.js', () => ({
  isSignedIn: () => false,
  isAuthRedirectCallback: () => false,
  handleRedirectCallback: async () => false,
  login: async () => {},
  subscribeTokenStored: () => () => {},
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.resetModules()
  delete globalThis.__LEAF_FLAGS
})

// `flagValue` undefined means the global is absent.
async function mountAppRoute(flagValue) {
  cleanup()
  vi.resetModules()
  if (flagValue === undefined) delete globalThis.__LEAF_FLAGS
  else globalThis.__LEAF_FLAGS = { oneShell: flagValue }
  window.history.pushState({}, '', '/app')
  const { default: SiteRoot } = await import('./SiteRoot.jsx')
  render(<SiteRoot />)
  // The console arm is lazy; its resolution proves the arm actually
  // MOUNTED, not merely that the wrapper rendered.
  await screen.findByTestId('app-stub')
}

describe('SiteRoot renders the one studio shell', () => {
  it('removes callback parameters before saving the page URL for a sign-in retry after timeout', async () => {
    window.history.replaceState({ page: 'drawing' }, '', '/app?surface=cad&code=x&state=y&drawing=roof#details')
    const auth = await import('../auth.js')
    vi.spyOn(auth, 'isAuthRedirectCallback').mockReturnValue(true)
    const callback = vi.spyOn(auth, 'handleRedirectCallback').mockImplementation(() => new Promise(() => {}))
    let returnTo
    const login = vi.spyOn(auth, 'login').mockImplementation(async () => {
      returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`
    })
    const { default: SiteRoot } = await import('./SiteRoot.jsx')
    vi.useFakeTimers()
    render(<SiteRoot />)
    expect(screen.getByRole('status').textContent).toBe('Completing sign in')
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(screen.getByRole('alert').textContent).toBe("Sign-in couldn't be completed.")
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in' })) })
    expect(callback).toHaveBeenCalledTimes(1)
    expect(login).toHaveBeenCalledTimes(1)
    expect(returnTo).toBe('/app?surface=cad&drawing=roof#details')
    const savedUrl = new URL(returnTo, window.location.origin)
    expect(savedUrl.searchParams.has('code')).toBe(false)
    expect(savedUrl.searchParams.has('state')).toBe(false)
    expect(window.history.state).toEqual({ page: 'drawing' })
  }, 20_000)

  // Cold dynamic imports of the SiteRoot graph after vi.resetModules() can take several seconds on a loaded runner.
  it('SSD1-B row1: a passive decision strip never blocks the Escape eject, an owned one still does', async () => {
    vi.resetModules()
    window.history.pushState({}, '', '/try')
    const router = await import('./router.js')
    const navigate = vi.spyOn(router, 'navigate').mockImplementation(() => {})
    const strip = document.createElement('div')
    strip.className = 'strip-decision'
    strip.setAttribute('data-escape-passive', 'true')
    try {
      const { default: SiteRoot } = await import('./SiteRoot.jsx')
      render(<SiteRoot />)
      expect(screen.getByTestId('stage-stub').getAttribute('data-scene')).toBe('tool')
      document.body.appendChild(strip)
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(navigate).toHaveBeenCalledTimes(1)
      expect(navigate).toHaveBeenCalledWith('/')

      navigate.mockClear()
      strip.removeAttribute('data-escape-passive')
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(navigate).not.toHaveBeenCalled()
    } finally {
      strip.remove()
      navigate.mockRestore()
    }
  }, 20_000)

  it('absent, 0 and 1 legacy flag values all mount the console INSIDE the one studio shell with its ground', async () => {
    for (const flagValue of [undefined, '0', '1']) {
      await mountAppRoute(flagValue)
      const label = `__LEAF_FLAGS ${flagValue === undefined ? 'absent' : flagValue}`
      const shells = document.querySelectorAll('.studio-shell[data-scene="app"]')
      expect(shells, label).toHaveLength(1)
      const shell = shells[0]
      expect(shell.getAttribute('data-mode'), label).toBe('console')
      const grounds = document.querySelectorAll('.studio-ground[role="region"]')
      expect(grounds, label).toHaveLength(1)
      expect(shell.contains(grounds[0]), label).toBe(true)
      expect(shell.contains(screen.getByTestId('app-stub')), label).toBe(true)
      expect(document.querySelector('.stage-stub'), label).toBeNull()
      cleanup()
    }
  }, 20_000)
})
