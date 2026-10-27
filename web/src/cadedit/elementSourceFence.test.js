// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'
import { bundleGraph, emittedJavaScript } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'
import elementSourceStamp, { elementRef, resolveElementRefs } from '../../vite-plugins/elementSourceStamp.js'

const MARKERS = [/data-element-source/g, /src\/[\w./-]+\.jsx(?::[A-Z][\w$]*)?/g, /src\/site\//g]
const REF = /["']data-element-ref["']\s*:\s*["']([^"'\\]*)["']/g

describe('element source stamp build fence', () => {
  let graph
  let productionText
  beforeAll(async () => {
    const directory = await nextFenceDirectory('enabled')
    graph = bundleGraph(directory)
    productionText = [...emittedJavaScript(directory).values()].join('\n')
  }, NEXT_FENCE_TIMEOUT)

  it('ships zero readable source stamps or source paths in the actual production export', () => {
    for (const marker of MARKERS) expect((productionText.match(marker) ?? []).length).toBe(0)
    expect(productionText).not.toContain('jsxDEV(')
  })

  it('ships opaque refs in reachable production chunks', () => {
    const values = [...productionText.matchAll(REF)].map(match => match[1])
    expect(values.length).toBeGreaterThan(0)
    for (const value of values) expect(value).toMatch(/^[0-9a-f]{12}$/)
    const chunks = [...graph.assets].filter(([file, code]) => file.endsWith('.js') && /["']data-element-ref["']\s*:/.test(code))
    expect(chunks.length).toBeGreaterThan(0)
    for (const [file] of chunks) expect(graph.reachable.has(file)).toBe(true)
  })

  it('resolves every emitted ref to the exact current JSX source hash', () => {
    const refs = [...new Set([...productionText.matchAll(REF)].map(match => match[1]))]
    expect(refs.length).toBeGreaterThan(0)
    const { matches, skipped } = resolveElementRefs(process.cwd(), refs)
    expect(skipped).toBe(0)
    for (const ref of refs) {
      const sources = matches.get(ref)
      expect(sources.length, `unresolved emitted element ref ${ref}`).toBeGreaterThan(0)
      for (const source of sources) expect(elementRef(source)).toBe(ref)
    }
  })

  it('the shared transform emits readable development stamps and the same opaque refs', () => {
    // Next dev is a server, not a production static-export variant. Exercise
    // its actual pre-loader transform against real current application JSX.
    const filename = join(process.cwd(), 'src/site/CockpitTopBand.jsx')
    const source = readFileSync(filename, 'utf8')
    const development = elementSourceStamp({ root: process.cwd(), source: true, ref: true }).transform(source, filename)
    const production = elementSourceStamp({ root: process.cwd(), source: false, ref: true }).transform(source, filename)
    expect(development).not.toBeNull()
    expect(production).not.toBeNull()
    const stamps = [...development.code.matchAll(/data-element-source="([^"]+)"/g)].map(match => match[1])
    const refs = code => [...code.matchAll(/data-element-ref="([^"]+)"/g)].map(match => match[1])
    expect(stamps.length).toBeGreaterThan(0)
    expect(refs(development.code)).toEqual(refs(production.code))
    expect(refs(production.code)).toEqual(stamps.map(elementRef))
    expect(production.code).not.toContain('data-element-source=')
  })

  it('keeps the application positive control in the inspected production output', () => {
    expect(productionText).toContain('tc-rail-body')
  })
})
