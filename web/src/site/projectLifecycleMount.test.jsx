import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

vi.mock('./router.js', () => ({ useRoute: () => ({ path: '/try' }), navigate: vi.fn() }))
vi.mock('./routeScene.js', () => ({ sceneForPath: () => 'tool' }))
vi.mock('./authBoot.js', () => ({ bootWantsApp: () => false, shouldDeferForAuthCallback: () => false }))
vi.mock('../auth.js', () => ({ handleRedirectCallback: vi.fn(), isSignedIn: () => true }))
vi.mock('./workbenchId.js', () => ({ liveDrawingId: () => 'drawing-1', rememberLiveDrawingId: vi.fn() }))
vi.mock('../api.js', () => ({
  getDrawingIntake: vi.fn(), getDrawingVersions: vi.fn(), redoDrawing: vi.fn(), undoDrawing: vi.fn(),
}))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({
  WorkspaceControllerProvider: ({ children }) => children,
}))
vi.mock('./StageLayer.jsx', () => ({ default: () => <div data-testid="stage" /> }))
vi.mock('./LandingCast.jsx', () => ({ default: () => <div data-cast="site" /> }))
vi.mock('./ToolCast.jsx', () => ({ default: () => <div data-cast="tool" /> }))
vi.mock('../projects/ProjectList.jsx', () => ({ default: () => <section aria-label="Projects" /> }))

import SiteRoot from './SiteRoot.jsx'

afterEach(cleanup)

describe('/try project lifecycle projection', () => {
  it('mounts the existing Projects surface inside the tool scene', () => {
    const { container } = render(<SiteRoot />)
    expect(container.querySelector('.stage-root')?.dataset.scene).toBe('tool')
    expect(screen.getByRole('region', { name: 'Projects' })).toBeTruthy()
    expect(container.querySelector('.projects-stage-panel')?.getAttribute('data-cast')).toBe('tool')
  })
})
