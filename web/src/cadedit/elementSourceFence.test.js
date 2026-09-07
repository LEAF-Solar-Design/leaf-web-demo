// @vitest-environment node
/** Ledger 9a: inspect real build bytes for the dev/staging source stamp. */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const WEB_ROOT = process.cwd()
const MARKERS = [/data-element-source/g, /src\/[\w./-]+\.jsx:[A-Z][\w$]*/g]

function buildWithMode(mode, fenceRoot) {
  const outDir = join(fenceRoot, mode)
  execFileSync(
    process.execPath,
    [join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--mode', mode, '--outDir', outDir, '--emptyOutDir'],
    {
      cwd: WEB_ROOT,
      env: { ...process.env, VITE_CAD_EDIT: '1' },
      stdio: 'pipe',
      timeout: 240_000,
    },
  )
  return outDir
}

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

function chunksContaining(chunks, outDir, marker) {
  const html = readFileSync(join(outDir, 'index.html'), 'utf8')
  const hits = []
  for (const [name, source] of chunks) {
    if (!source.match(marker)) continue
    const referencedBy = [...chunks]
      .filter(([other, source2]) => other !== name && source2.includes(name))
      .map(([other]) => other)
    if (html.includes(name)) referencedBy.push('index.html')
    hits.push({ name, referencedBy })
  }
  return hits
}

describe('element source stamp build fence', () => {
  let fenceRoot
  let stagingDir
  let stagingChunks
  let productionChunks

  beforeAll(() => {
    // Unique outputs also isolate concurrent worktrees sharing node_modules.
    fenceRoot = mkdtempSync(join(tmpdir(), 'leaf-element-source-fence-'))
    stagingDir = buildWithMode('staging', fenceRoot)
    stagingChunks = emittedJavaScript(stagingDir)
    productionChunks = emittedJavaScript(buildWithMode('production', fenceRoot))
  }, 600_000)

  afterAll(() => {
    if (fenceRoot) rmSync(fenceRoot, { recursive: true, force: true })
  })

  it('ships source attributes and source stamp values in staging', () => {
    for (const marker of MARKERS) expect((allText(stagingChunks).match(marker) ?? []).length).toBeGreaterThan(0)
  })

  it('ships staging stamps in reachable chunks', () => {
    for (const marker of MARKERS) {
      const hits = chunksContaining(stagingChunks, stagingDir, marker)
      expect(hits.length).toBeGreaterThan(0)
      for (const hit of hits) expect(hit.referencedBy.length).toBeGreaterThan(0)
    }
  })

  it('ships zero source attributes or source stamp values in production', () => {
    for (const marker of MARKERS) expect((allText(productionChunks).match(marker) ?? []).length).toBe(0)
  })

  it('keeps the app positive control in both builds', () => {
    expect(allText(stagingChunks)).toContain('tc-rail-body')
    expect(allText(productionChunks)).toContain('tc-rail-body')
  })
})
