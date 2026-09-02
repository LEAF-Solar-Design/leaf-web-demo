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
 * Slow by nature (one full vite build) — same justification as the two
 * bundleFence suites this copies its harness from.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
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

describe('one-shell runtime rail bundle shape', () => {
  let js = ''
  beforeAll(() => {
    build()
    js = emittedJavaScript()
  }, 300_000)

  it('ships the studio shell', () => {
    expect(js).toContain('studio-shell')
    expect(js).toContain('studio-ground')
  })

  it('ships the old console shell beside it (rollback lives in the same artifact)', () => {
    // The console's own furniture — present regardless of the rail, because
    // the rail is a runtime branch, not a build fence.
    expect(js).toContain('workspace-card')
    expect(js).toContain('viewer-wrap')
  })

  it('reads the flag at RUNTIME — the __LEAF_FLAGS read survives un-folded', () => {
    expect(js).toContain('__LEAF_FLAGS')
    expect(js).toContain('oneShell')
  })

  it('never grew a VITE-shaped one-shell flag', () => {
    expect(js).not.toContain('VITE_ONE_SHELL')
  })
})
