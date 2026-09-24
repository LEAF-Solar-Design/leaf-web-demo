/**
 * Bundle-shape receipt for the ONE studio shell (ACCEPTANCE.md, W7 and its
 * Version 3 section).
 *
 * W7 deleted the old console shell and the runtime rail that selected it, so
 * the app always renders the studio shell. One real vite build, then prove on
 * the emitted artifact that (a) the studio shell actually ships and (b) no
 * read of the legacy `__LEAF_FLAGS` global survives anywhere in the bundle:
 * nothing shipped can select a different shell.
 *
 * Panel-hardened: the "no VITE-shaped flag" guard is a SOURCE scan, because
 * Vite define-replaces `import.meta.env.VITE_ONE_SHELL` at build time and
 * the literal never reaches dist JS exactly when the fence regression is
 * real; and the studio-ground marker excludes App's `studio-ground-viewer`
 * substring.
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

describe('one studio shell bundle shape', () => {
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

  it('carries no read of the legacy __LEAF_FLAGS global — nothing shipped can select another shell', () => {
    expect(js).not.toContain('__LEAF_FLAGS')
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
