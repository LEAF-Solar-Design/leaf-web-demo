// @vitest-environment jsdom
//
// The ARM-SWAP receipt (W4c-0 debt, ACCEPTANCE deferred list): a real jsdom
// mount of SiteRoot under BOTH `__LEAF_FLAGS` values. The source pins in
// siteRootOneShell.test.js prove the two arms EXIST in the file; only a
// render proves the flag picks the right one — a dodge that renders the
// studio arm under rail OFF (or swaps the arms) passes every source pin and
// fails here.
//
// The heavy subtrees are stubbed at their module seams (three.js cannot run
// under jsdom); the REAL modules under test are SiteRoot's branch logic and
// runtimeFlags' module-eval read — which is why every case resets the module
// registry and sets the flag global BEFORE the dynamic import.
import { cleanup, render, screen } from '@testing-library/react'
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
  subscribeTokenStored: () => () => {},
}))

afterEach(() => {
  cleanup()
  vi.resetModules()
  delete globalThis.__LEAF_FLAGS
})

async function mountAppRoute(flagValue) {
  vi.resetModules()
  globalThis.__LEAF_FLAGS = { oneShell: flagValue }
  window.history.pushState({}, '', '/app')
  const { default: SiteRoot } = await import('./SiteRoot.jsx')
  render(<SiteRoot />)
  // The console arm is lazy in both shells; its resolution proves the arm
  // actually MOUNTED, not merely that the wrapper rendered.
  await screen.findByTestId('app-stub')
}

describe('SiteRoot arm swap under the one-shell rail', () => {
  it("rail '1' mounts the console INSIDE the studio shell with its ground", async () => {
    await mountAppRoute('1')
    const shell = document.querySelector('.studio-shell[data-mode="console"]')
    expect(shell).toBeTruthy()
    expect(shell.querySelector('.studio-ground')).toBeTruthy()
    expect(shell.contains(screen.getByTestId('app-stub'))).toBe(true)
    expect(document.querySelector('.stage-stub')).toBeNull()
  })

  it("rail '0' renders the console alone: no studio DOM anywhere", async () => {
    await mountAppRoute('0')
    expect(document.querySelector('.studio-shell')).toBeNull()
    expect(document.querySelector('.studio-ground')).toBeNull()
    expect(screen.getByTestId('app-stub')).toBeTruthy()
    expect(document.querySelector('.stage-stub')).toBeNull()
  })

  it('an absent flags global fails closed to the old shell', async () => {
    vi.resetModules()
    delete globalThis.__LEAF_FLAGS
    window.history.pushState({}, '', '/app')
    const { default: SiteRoot } = await import('./SiteRoot.jsx')
    render(<SiteRoot />)
    await screen.findByTestId('app-stub')
    expect(document.querySelector('.studio-shell')).toBeNull()
  })
})
