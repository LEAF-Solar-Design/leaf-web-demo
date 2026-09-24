// @vitest-environment node
//
// Source pins for the W3 one-shell mount (docs/convergence/ACCEPTANCE.md).
// Each pin names a contract whose loss is silent at build time: the rollback
// arm drifting from the shipped shell, the auth-callback deferral slipping
// below the shell branch, the portal regrowing a null-ground inline Viewer, or
// the studio shell adopting the dark-token .stage-root class before W4's
// re-pin work. Normalized to LF (Windows checkout is CRLF) and pinned on
// COMMENT-STRIPPED source, so a commented-out copy of the required code can
// never satisfy a pin after the live code is deleted (panel finding).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const raw = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8').replace(/\r\n/g, '\n')

// Strip block comments, JSX comment braces, and full-line // comments. Not a
// parser: it leaves `//` inside string literals (URLs) alone by only removing
// lines whose first non-space characters are `//`.
export function stripComments(src) {
  return src
    .replace(/\{\s*\/\*[\s\S]*?\*\/\s*\}/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

const read = (rel) => stripComments(raw(rel))

const viewerPortalPattern = /studioGround\s*\?\s*createPortal\(<div className="studio-ground-viewer"[\s\S]{0,600}?\bhidden=\{effectiveGround !== 'drawing' && leavingGround !== 'drawing'\}[\s\S]{0,600}?>\{viewerEl\}<\/div>,\s*studioGround\)\s*:\s*null/

// The studio-shell JSX block: from the host div to the sheets arm.
function studioArm(src) {
  const start = src.indexOf('<div className="studio-shell"')
  const end = src.indexOf(") : scene === 'sheets'", start)
  expect(start).toBeGreaterThan(-1)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

// W7 deleted the rail. The deleted module and constant are spelled by parts
// so this file never carries the retired names verbatim.
const FLAG_MODULE = ['runtime', 'Flags'].join('')
const FLAG_CONST = ['ONE', 'SHELL', 'ENABLED'].join('_')
// Scene 'app' renders the studio shell DIRECTLY: the host div is the arm's
// first element, with no condition between the scene test and the markup.
const directStudioArm = /scene === 'app' \? \(\s*\n\s*<div className="studio-shell" data-scene="app" data-mode="console">/

describe('SiteRoot one-shell branch', () => {
  const src = read('./SiteRoot.jsx')

  it('renders the studio shell for scene app unconditionally: no rail import, no flag', () => {
    expect(raw('./SiteRoot.jsx')).not.toContain(FLAG_MODULE)
    expect(raw('./SiteRoot.jsx')).not.toContain(FLAG_CONST)
    expect(src).toMatch(directStudioArm)
  })

  it('mounts ONE console workspace provider — the factory literal, once', () => {
    const mounts = src.match(/<WorkspaceControllerProvider \{\.\.\.consoleWorkspaceMount\(\)\}>/g) || []
    expect(mounts).toHaveLength(1)
  })

  it('the studio shell declares scene and mode for the route-matrix receipts', () => {
    expect(src).toMatch(/className="studio-shell" data-scene="app" data-mode="console"/)
  })

  it('is NOT .stage-root in ANY spelling — the dark token re-pin would flip the console identity', () => {
    // Scoped to the studio arm's JSX (not one literal attribute order): no
    // class expression, clsx call, or reordered attribute may carry the
    // stage class until W4 lands the console-mode token re-pin.
    expect(studioArm(src)).not.toMatch(/stage-root/)
  })

  it('the ground node is a named landmark, hidden only while empty', () => {
    expect(studioArm(src)).toMatch(/className="studio-ground" ref=\{setStudioGround\} role="region" aria-label="Drawing" aria-hidden=\{studioGround \? undefined : true\}/)
  })

  it('keeps the auth-callback deferral ABOVE the shell branch (never regress the deferral)', () => {
    const deferral = src.indexOf('if (authCallbackPending) return')
    const branch = src.indexOf('<div className="studio-shell"')
    expect(deferral).toBeGreaterThan(-1)
    expect(branch).toBeGreaterThan(-1)
    expect(deferral).toBeLessThan(branch)
  })

  it('hands the ground to App as STATE, not a ref (the portal must see attachment)', () => {
    expect(src).toMatch(/const \[studioGround, setStudioGround\] = useState\(null\)/)
    expect(src).toMatch(/<StudioGroundContext\.Provider value=\{studioGround\}>/)
  })
})

describe('App portal wiring', () => {
  const src = read('../App.jsx')

  it('renders the Viewer through the ground portal ONLY when a ground exists', () => {
    expect(src).toMatch(/const studioGround = useStudioGround\(\)/)
    expect(src).toMatch(viewerPortalPattern)
  })

  it('mounts the surface grounds ONLY through the ground portal (rail OFF has no ground, so none of it)', () => {
    // W4a: the project board and device stage are studio-only by
    // construction. One JSX occurrence, and it sits inside the
    // `studioGround && createPortal(` guard — never rendered inline.
    const occurrences = src.match(/<SurfaceGrounds/g) || []
    expect(occurrences).toHaveLength(1)
    expect(src).toMatch(/\{studioGround && createPortal\(\s*\n?\s*<SurfaceGrounds/)
    expect(src).toMatch(/import SurfaceGrounds, \{ groundShowsDrawing \} from '\.\/site\/SurfaceGrounds\.jsx'/)
  })

  it('the grounded viewer goes transparent; the inline viewer keeps its default', () => {
    expect(src).toMatch(/background=\{studioGround \? 'transparent' : undefined\}/)
  })

  it('mounts the cockpit ONLY under the studio ground, and the surface attribute only there', () => {
    // W4b: view cluster and status cluster are studio chrome; the
    // data-surface hook the studio CSS scopes on exists only under the rail,
    // so the old shell's DOM stays byte-for-byte.
    expect((src.match(/<ViewCluster/g) || [])).toHaveLength(1)
    expect((src.match(/<CockpitStatus/g) || [])).toHaveLength(1)
    expect(src).toMatch(/\{studioGround && intake && groundShowsDrawing\(activeSurface\) && \(\s*\n?\s*<ViewCluster/)
    expect(src).toMatch(/\{studioGround && groundShowsDrawing\(activeSurface\) && \(\s*\n?\s*<CockpitStatus/)
    // (`data-tour="shell"` after it is slice 4b's console tour anchor; the
    // surface attribute's gate is what this pin guards.)
    expect(src).toContain("const studioShell = !!studioGround && surfaceSlots.chrome.shell === 'cockpit'")
    expect(src).toMatch(/data-studio-shell=\{studioShell \? 'cockpit' : undefined\} data-surface=/)
    expect(src).toMatch(/<div className="app"[^>]*\bdata-surface=\{studioGround \? activeSurface : undefined\} data-start-open=\{studioGround && startOpen \? 'true' : undefined\} data-tour="shell"\s+onClickCapture=/)
  })

  it('never seeds intake synchronously — the single-mount invariant of the portal', () => {
    // The ground attaches in a pre-paint state flush, before any async intake
    // resolves, so the Viewer's first committed render is already the portal
    // render. An `initialIntake` passed to the version controller would make
    // intake truthy at App's FIRST render, mount the Viewer inline, then
    // remount it into the ground one commit later (WebGL context rebuild,
    // camera pose lost). Pinned here because nothing at runtime would fail.
    expect(src).not.toMatch(/initialIntake/)
  })

  it('keeps the phone drawer state inside the studio shell with a bounded default', () => {
    expect(src).toContain("const STUDIO_DRAWERS = Object.freeze(['nav', 'jobs', 'result', 'plan', 'none'])")
    expect(src).toContain("const [studioDrawer, setStudioDrawer] = useState('none')")
    expect(src).toContain("STUDIO_DRAWERS.includes(name) ? name : 'none'")
    expect(src).toContain('data-drawer={studioShell ? studioDrawer : undefined}')
    expect(src).toMatch(/\{studioShell && \(\s*<div className="studio-drawer-tabs"/)
  })
})

describe('rollback contract', () => {
  it('the ground context defaults to null — no provider means the old shell', () => {
    const src = read('./studioGround.js')
    expect(src).toMatch(/createContext\(null\)/)
  })

  it('the studio shell styles live in landing.css (dark file), never styles.css', () => {
    expect(raw('./landing.css')).toMatch(/\.studio-shell \{/)
    expect(raw('../styles.css')).not.toMatch(/studio-shell/)
  })

  it('every pointer RESTORE in the studio chain is zero-specificity (:where), every link plain', () => {
    // A restore rule with specificity beats an element's own declared
    // `pointer-events: none` (tour root, drawer layer, .exit) into a click
    // shield, and a `> *` restore out-specifies the next `none` link; both
    // were live defects. Pin the shape: no bare `... > * { pointer-events:
    // auto }` in the block, and each chain link declares `none`.
    const css = stripComments(raw('./landing.css'))
    const block = css.slice(css.indexOf('.studio-shell {'))
    expect(block).not.toMatch(/^[^:\n][^\n]*> \* \{[^}]*pointer-events: auto/m)
    for (const link of ['.studio-shell .app', '.studio-shell .center-col', '.studio-shell main.center-scroll', '.studio-shell .workspace-card', '.studio-shell .viewer-wrap']) {
      const rule = block.slice(block.indexOf(`${link} {`))
      expect(rule.slice(0, rule.indexOf('}')), link).toMatch(/pointer-events: none/)
    }
    for (const restore of ['.app > *', '.center-col > *', 'main.center-scroll > *', '.workspace-card > *', '.viewer-wrap > *']) {
      // Literal rule text, not a regex built from the selector (CodeQL
      // js/incomplete-sanitization on the escape helper, and a literal is
      // the stronger pin anyway).
      expect(block, restore).toContain(`:where(.studio-shell ${restore}) { pointer-events: auto; }`)
    }
  })
})

// Falsification: the pins must FAIL on the shapes they forbid.
describe('falsification', () => {
  it('a studio arm wrapped back in a condition fails the direct-render pin', () => {
    const src = read('./SiteRoot.jsx')
    expect(src).toMatch(directStudioArm)
    const mutated = src.replace('<div className="studio-shell"', 'legacyShell ? (\n          <div className="studio-shell"')
    expect(mutated).not.toBe(src)
    expect(mutated).not.toMatch(directStudioArm)
  })

  it('a portal that regrows the null-ground inline Viewer fails the portal pin', () => {
    const src = read('../App.jsx')
    expect(src).toMatch(viewerPortalPattern)
    // Anchored on the portal's own tail: App.jsx carries many other `: null`.
    const mutated = src.replace(/(>\{viewerEl\}<\/div>,\s*studioGround\)\s*): null/, '$1: viewerEl')
    expect(mutated).not.toBe(src)
    expect(mutated).not.toMatch(viewerPortalPattern)
  })

  it('an inline (unguarded) SurfaceGrounds fails the portal-only pin', () => {
    const mutated = read('../App.jsx').replace(/\{studioGround && createPortal\(\s*\n\s*<SurfaceGrounds/, '{createPortal(\n          <SurfaceGrounds')
    expect(mutated).not.toMatch(/\{studioGround && createPortal\(\s*\n?\s*<SurfaceGrounds/)
  })

  it('a commented-out deferral no longer satisfies the deferral pin', () => {
    const mutated = stripComments(raw('./SiteRoot.jsx').replace('if (authCallbackPending) return', '// if (authCallbackPending) return'))
    expect(mutated.indexOf('if (authCallbackPending) return')).toBe(-1)
  })

  it('a clsx-spelled stage-root on the studio host fails the scoped pin', () => {
    const mutated = read('./SiteRoot.jsx').replace('className="studio-shell"', "className={cx('stage-root', 'studio-shell')}")
    expect(mutated).toMatch(/stage-root/)
    // The scoped pin reads the arm from the host div; a renamed host is the
    // pin's own boundary moving, which the mode/scene pin above catches.
    const start = mutated.indexOf("className={cx('stage-root', 'studio-shell')}")
    const end = mutated.indexOf(') : (', start)
    expect(mutated.slice(start, end)).toMatch(/stage-root/)
  })

  it('a specific restore rule fails the zero-specificity pin', () => {
    const css = stripComments(raw('./landing.css'))
    const block = css.slice(css.indexOf('.studio-shell {')).replace(':where(.studio-shell .app > *) { pointer-events: auto; }', '.studio-shell .app > * { pointer-events: auto; }')
    expect(block).toMatch(/^[^:\n][^\n]*> \* \{[^}]*pointer-events: auto/m)
  })
})
