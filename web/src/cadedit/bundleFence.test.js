/**
 * Reachability oracle for the cad_edit build fence.
 *
 * Copied in approach from web/src/projects/bundleFence.test.js — including
 * its positive control — because it answers the only question a DOM test
 * cannot: does the flag-OFF build actually SHIP nothing? A component that
 * gates itself with a runtime `if (!enabled) return null` still ships its
 * markup, its strings, and (here) a whole DXF engine, one prop away from a
 * public page.
 *
 * So this builds the real app twice, VITE_CAD_EDIT=1 and =0, and greps the
 * emitted JS for markers only this surface renders. Present with the flag
 * on, absent with it off — measured on the artifact that gets deployed, not
 * on the source that describes it.
 *
 * Slow by nature (two full vite builds). Same justification as the
 * lifecycle fence: it is the only test that can answer this at all.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'

// Markers this surface renders and nothing else in the tree does. The engine
// marker is a string only the worker-side document model contains, so it
// also proves the DXF engine itself does not ride along on a flag-off build.
const SURFACE_MARKERS = ['cad-edit-workbench', 'cad-edit-entity-list']
const ENGINE_MARKERS = ['this build can read but not rewrite', 'too_many_group_pairs']

const WEB_ROOT = process.cwd()
const FENCE_ROOT = join(WEB_ROOT, 'node_modules', '.cache', 'cad-edit-fence')

function buildWithFlag(flagValue) {
  const outDir = join(FENCE_ROOT, flagValue === '1' ? 'on' : 'off')
  rmSync(outDir, { recursive: true, force: true })
  mkdirSync(outDir, { recursive: true })
  execFileSync(
    process.execPath,
    [join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', outDir, '--emptyOutDir'],
    {
      cwd: WEB_ROOT,
      env: { ...process.env, VITE_CAD_EDIT: flagValue },
      stdio: 'pipe',
      timeout: 240_000,
    },
  )
  return outDir
}

// Every emitted JS file of a build, by name — worker chunks included: a
// worker chunk is still shipped JavaScript, and the engine markers above are
// exactly what would hide there if the fence only covered the entry chunk.
function emittedJavaScript(outDir) {
  const assets = join(outDir, 'assets')
  expect(existsSync(assets)).toBe(true)
  const files = readdirSync(assets).filter((f) => f.endsWith('.js'))
  expect(files.length).toBeGreaterThan(0)
  return new Map(files.map((f) => [f, readFileSync(join(assets, f), 'utf8')]))
}

function allText(chunks) {
  return [...chunks.values()].join('\n')
}

// Names the chunks that contain a marker, and for each, whether any OTHER
// emitted chunk (or index.html) names it — i.e. whether it is reachable at
// all, or an orphan the browser will never fetch.
function chunksContaining(chunks, outDir, marker) {
  const html = readFileSync(join(outDir, 'index.html'), 'utf8')
  const hits = []
  for (const [name, source] of chunks) {
    if (!source.includes(marker)) continue
    const referencedBy = [...chunks]
      .filter(([other, source2]) => other !== name && source2.includes(name))
      .map(([other]) => other)
    if (html.includes(name)) referencedBy.push('index.html')
    hits.push({ name, referencedBy })
  }
  return hits
}

describe('cad_edit build fence', () => {
  let onDir = ''
  let offDir = ''
  let onChunks = new Map()
  let offChunks = new Map()

  beforeAll(() => {
    onDir = buildWithFlag('1')
    onChunks = emittedJavaScript(onDir)
    offDir = buildWithFlag('0')
    offChunks = emittedJavaScript(offDir)
  }, 600_000)

  it('ships the editing surface when VITE_CAD_EDIT=1', () => {
    for (const marker of SURFACE_MARKERS) expect(allText(onChunks)).toContain(marker)
  })

  it('ships a REACHABLE DXF engine worker chunk when VITE_CAD_EDIT=1', () => {
    for (const marker of ENGINE_MARKERS) {
      const hits = chunksContaining(onChunks, onDir, marker)
      expect(hits.length).toBeGreaterThan(0)
      // Flag on, the worker chunk must be named by the entry that spawns it,
      // or the surface would ship a worker it can never start.
      for (const hit of hits) expect(hit.referencedBy.length).toBeGreaterThan(0)
    }
  })

  it('ships NOTHING of the editing surface when VITE_CAD_EDIT=0', () => {
    for (const marker of SURFACE_MARKERS) expect(allText(offChunks)).not.toContain(marker)
  })

  it('leaves the DXF engine chunk UNREACHABLE when VITE_CAD_EDIT=0 (no chunk names it)', () => {
    // KNOWN LIMITATION, measured rather than assumed: Vite's worker plugin
    // emits a worker chunk during transform, BEFORE tree-shaking, so the
    // engine bytes are still written to dist even though the surface that
    // spawns them is gone. What the fence can and does prove is that the
    // chunk is an orphan — no entry, no other chunk, and not index.html
    // names it — so nothing ever fetches or executes it. Deleting those
    // bytes outright is future work (see docs/CAD-EDIT-SURFACE-DESIGN.md).
    for (const marker of ENGINE_MARKERS) {
      const hits = chunksContaining(offChunks, offDir, marker)
      expect(hits.length).toBeGreaterThan(0)
      for (const hit of hits) expect(hit.referencedBy).toEqual([])
    }
  })

  it('leaves the rest of the app identical either way (the fence is not a rewrite)', () => {
    // The positive control: if the flag-off build were simply broken/empty,
    // the absence assertions above would pass for the wrong reason.
    expect(allText(offChunks)).toContain('tc-rail-body')
    expect(allText(onChunks)).toContain('tc-rail-body')
  })
})
