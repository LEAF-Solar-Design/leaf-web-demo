/**
 * Reachability oracle for the lifecycle_ui build fence.
 *
 * A DOM test can only prove the panel renders or does not render for the props
 * it was handed. It cannot prove the flag-off build actually SHIPS nothing —
 * and the previous mount got exactly that wrong: ProjectList gated itself with
 * a runtime `if (!enabled) return null`, so with the flag off the component,
 * its markup and its strings were still in the production bundle, one prop away
 * from a public page. `grep projects-surface dist/assets/*.js` on a flag-off
 * build of commit 35ee8c0b finds it.
 *
 * So this test builds the real app twice, with VITE_LIFECYCLE_UI=1 and =0, and
 * greps the emitted JS for stable markers the panel renders. Present with the
 * flag on, absent with it off — that is the fence, measured on the artifact
 * that actually gets deployed rather than on the source that describes it.
 *
 * Slow by nature (two full vite builds, ~20s each). It is the only test in this
 * suite that is allowed to be, because it is the only one that can answer the
 * question at all.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'

// Markers the panel renders and nothing else in the tree does.
const MARKERS = ['projects-surface', 'membership-panel']

// vitest's root is web/ (vitest.config.js); import.meta.url is not a file: URL
// under the jsdom transform, so the root is taken from the process instead.
const WEB_ROOT = process.cwd()
// Inside the project root so vite does not refuse to empty it, and inside
// node_modules/.cache so it is never a build output anybody ships or commits.
const FENCE_ROOT = join(WEB_ROOT, 'node_modules', '.cache', 'lifecycle-fence')

function buildWithFlag(flagValue) {
  const outDir = join(FENCE_ROOT, flagValue === '1' ? 'on' : 'off')
  rmSync(outDir, { recursive: true, force: true })
  mkdirSync(outDir, { recursive: true })
  execFileSync(
    process.execPath,
    [join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', outDir, '--emptyOutDir'],
    {
      cwd: WEB_ROOT,
      env: { ...process.env, VITE_LIFECYCLE_UI: flagValue },
      stdio: 'pipe',
      timeout: 240_000,
    },
  )
  return outDir
}

// Concatenated JS of a build. CSS is excluded on purpose: a class name that
// survives in a stylesheet says nothing about whether the component ships.
function bundledJavaScript(outDir) {
  const assets = join(outDir, 'assets')
  expect(existsSync(assets)).toBe(true)
  const files = readdirSync(assets).filter((f) => f.endsWith('.js'))
  expect(files.length).toBeGreaterThan(0)
  return files.map((f) => readFileSync(join(assets, f), 'utf8')).join('\n')
}

describe('lifecycle_ui build fence', () => {
  let withFlag = ''
  let withoutFlag = ''

  beforeAll(() => {
    withFlag = bundledJavaScript(buildWithFlag('1'))
    withoutFlag = bundledJavaScript(buildWithFlag('0'))
  }, 600_000)

  it('ships the lifecycle surface when VITE_LIFECYCLE_UI=1', () => {
    for (const marker of MARKERS) expect(withFlag).toContain(marker)
  })

  it('ships NOTHING of the lifecycle surface when VITE_LIFECYCLE_UI=0', () => {
    for (const marker of MARKERS) expect(withoutFlag).not.toContain(marker)
  })

  it('leaves the rest of the app identical either way (the fence is not a rewrite)', () => {
    // A sanity control: if the flag-off build were simply broken/empty, the
    // absence assertion above would pass for the wrong reason.
    expect(withoutFlag).toContain('tc-rail-body')
    expect(withFlag).toContain('tc-rail-body')
  })
})
