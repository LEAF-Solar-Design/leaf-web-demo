/**
 * Bundle-shape receipt for the one-shell RUNTIME rail (ACCEPTANCE.md flag
 * matrix, one-shell row).
 *
 * The contract asked for a "build-twice bundle-shape test" when the rail's
 * mechanism was undecided. The ratified rail is RUNTIME
 * (LEAF_ONE_SHELL_ENABLED on the task definition; one shared image serves
 * staging and production), so there is nothing to vary between two builds —
 * BOTH shells ship in ONE bundle and the branch happens in the browser. The
 * build-shape receipt that carries the same intent is therefore: one real
 * vite build, then prove on the emitted artifact that (a) the studio shell
 * actually ships, (b) the old shell still ships beside it, and (c) the
 * runtime flag READ survives into the bundle un-folded — if Vite ever
 * constant-folded ONE_SHELL_ENABLED, the deployed rail would be inert and
 * every environment would be pinned to whatever the build machine had.
 *
 * Panel-hardened: the "no VITE-shaped flag" guard is a SOURCE scan, because
 * Vite define-replaces `import.meta.env.VITE_ONE_SHELL` at build time and
 * the literal never reaches dist JS exactly when the fence regression is
 * real; the runtime-read guard asserts the COMPARISON survives (property
 * name adjacent to the `'1'` literal), not just two property names; and the
 * studio-ground marker excludes App's `studio-ground-viewer` substring.
 *
 * Slow by nature (one full vite build) — same justification as the two
 * bundleFence suites this copies its harness from.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'

const WEB_ROOT = process.cwd()
const OUT_ROOT = join(WEB_ROOT, 'node_modules', '.cache', 'one-shell-shape')

function build() {
  rmSync(OUT_ROOT, { recursive: true, force: true })
  mkdirSync(OUT_ROOT, { recursive: true })
  execFileSync(
    process.execPath,
    [join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', OUT_ROOT, '--emptyOutDir'],
    { cwd: WEB_ROOT, env: { ...process.env }, stdio: 'pipe', timeout: 240_000 },
  )
}

function emittedJavaScript() {
  const assets = join(OUT_ROOT, 'assets')
  expect(existsSync(assets)).toBe(true)
  const files = readdirSync(assets).filter((f) => f.endsWith('.js'))
  expect(files.length).toBeGreaterThan(0)
  return files.map((f) => readFileSync(join(assets, f), 'utf8')).join('\n')
}

// Every source file the bundle is built from, plus the container and CI
// surfaces a build-arg fence would have to touch (ACCEPTANCE flag matrix:
// "any build-arg change touches FOUR files").
function sourceFiles() {
  const out = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) { if (name !== 'node_modules') walk(p) }
      else if (/\.(jsx?|mjs|cjs|css|html)$/.test(name)) out.push(p)
    }
  }
  walk(join(WEB_ROOT, 'src'))
  out.push(join(WEB_ROOT, 'index.html'))
  const repo = join(WEB_ROOT, '..')
  for (const rel of ['deploy/Dockerfile.web', '.github/workflows/build-platform-images.yml']) {
    const p = join(repo, rel)
    if (existsSync(p)) out.push(p)
  }
  return out
}

describe('one-shell runtime rail bundle shape', () => {
  let js = ''
  beforeAll(() => {
    build()
    js = emittedJavaScript()
  }, 300_000)

  it('ships the studio shell — the host AND SiteRoot\'s ground node, not just App\'s portal wrapper', () => {
    expect(js).toContain('studio-shell')
    expect(js).toMatch(/studio-ground(?!-viewer)["'\s]/)
    expect(js).toContain('studio-ground-viewer')
  })

  it('ships the old console shell beside it (rollback lives in the same artifact)', () => {
    // The console's own furniture ships regardless of the rail (a runtime
    // branch, not a build fence). Which ARM renders under which flag value
    // is a behavior, proven by the managed e2e ON/OFF rows, not a string.
    expect(js).toContain('workspace-card')
    expect(js).toContain('viewer-wrap')
  })

  it('reads the flag at RUNTIME — the __LEAF_FLAGS comparison survives un-folded', () => {
    expect(js).toContain('__LEAF_FLAGS')
    // The read itself: `oneShell` adjacent to the `=== '1'` comparison in
    // whatever shape the minifier leaves (optional-chaining lowered or not).
    expect(js).toMatch(/oneShell[^a-zA-Z_$]{0,12}===?\s*["']1["']/)
  })

  it('never grew a VITE-shaped one-shell flag (source scan — a defined flag is erased from dist)', () => {
    const self = join(WEB_ROOT, 'src', 'site', 'oneShellBundleShape.test.js')
    const hits = sourceFiles()
      .filter((p) => p !== self)
      .filter((p) => readFileSync(p, 'utf8').includes('VITE_ONE_SHELL'))
      .map((p) => relative(WEB_ROOT, p))
    expect(hits).toEqual([])
    expect(js).not.toContain('VITE_ONE_SHELL')
  })
})
