// @vitest-environment node
//
// Source pins for the W3 one-shell mount (docs/convergence/ACCEPTANCE.md).
// Each pin names a contract whose loss is silent at build time: the rollback
// arm drifting from the shipped shell, the auth-callback deferral slipping
// below the shell branch, the portal losing its null-ground inline path, or
// the studio shell adopting the dark-token .stage-root class before W4's
// re-pin work. Normalized to LF (Windows checkout is CRLF).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8').replace(/\r\n/g, '\n')

describe('SiteRoot one-shell branch', () => {
  const src = read('./SiteRoot.jsx')

  it('branches the app arm on the runtime rail, imported from runtimeFlags', () => {
    expect(src).toMatch(/import \{ ONE_SHELL_ENABLED \} from '\.\.\/lib\/runtimeFlags\.js'/)
    expect(src).toMatch(/scene === 'app' \? \(\s*\n\s*ONE_SHELL_ENABLED \? \(/)
  })

  it('mounts ONE console workspace provider in EACH arm — the factory literal, twice', () => {
    const mounts = src.match(/<WorkspaceControllerProvider \{\.\.\.consoleWorkspaceMount\(\)\}>/g) || []
    expect(mounts).toHaveLength(2)
  })

  it('the studio shell declares scene and mode for the route-matrix receipts', () => {
    expect(src).toMatch(/className="studio-shell" data-scene="app" data-mode="console"/)
  })

  it('is NOT .stage-root — the dark token re-pin would flip the console identity', () => {
    // The studio shell must not adopt the stage class until W4 lands the
    // console-mode token re-pin; landing.css re-pins the full dark set on
    // .stage-root. Pin the className expression, not prose.
    expect(src).not.toMatch(/className="stage-root[^"]*" data-scene="app"/)
  })

  it('keeps the auth-callback deferral ABOVE the shell branch (never regress the deferral)', () => {
    const deferral = src.indexOf('if (authCallbackPending) return')
    const branch = src.indexOf('ONE_SHELL_ENABLED ?')
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
    expect(src).toMatch(/studioGround\s*\n?\s*\? createPortal\(<div className="studio-ground-viewer">\{viewerEl\}<\/div>, studioGround\)\s*\n?\s*: viewerEl/)
  })

  it('the grounded viewer goes transparent; the inline viewer keeps its default', () => {
    expect(src).toMatch(/background=\{studioGround \? 'transparent' : undefined\}/)
  })
})

describe('rollback contract', () => {
  it('the ground context defaults to null — no provider means the old shell', () => {
    const src = read('./studioGround.js')
    expect(src).toMatch(/createContext\(null\)/)
  })

  it('the studio shell styles live in landing.css (dark file), never styles.css', () => {
    expect(read('./landing.css')).toMatch(/\.studio-shell \{/)
    expect(read('../styles.css')).not.toMatch(/studio-shell/)
  })
})

// Falsification: the branch pin must FAIL on the shape it forbids.
describe('falsification', () => {
  it('an unconditional studio shell (no rail branch) fails the branch pin', () => {
    const mutated = read('./SiteRoot.jsx').replace('ONE_SHELL_ENABLED ? (', 'true ? (')
    expect(mutated).not.toMatch(/scene === 'app' \? \(\s*\n\s*ONE_SHELL_ENABLED \? \(/)
  })

  it('a portal without the null-ground inline fallback fails the portal pin', () => {
    const mutated = read('../App.jsx').replace(': viewerEl', ': null')
    expect(mutated).not.toMatch(/\? createPortal\(<div className="studio-ground-viewer">\{viewerEl\}<\/div>, studioGround\)\s*\n?\s*: viewerEl/)
  })
})
