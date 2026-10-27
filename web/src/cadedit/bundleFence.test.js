import { beforeAll, describe, expect, it } from 'vitest'
import { bundleGraph } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'

const SURFACE_MARKERS = ['cad-edit-workbench', 'cad-edit-entity-list']
const ENGINE_MARKERS = ['engine_unavailable', 'bad_document_bytes']
const text = graph => [...graph.assets].filter(([file]) => file.endsWith('.js')).map(([, code]) => code).join('\n')
describe('cad_edit build fence', () => {
  let on
  let off
  beforeAll(async () => {
    on = bundleGraph(await nextFenceDirectory('enabled'))
    off = bundleGraph(await nextFenceDirectory('disabled'))
  }, NEXT_FENCE_TIMEOUT)
  it('ships the editing surface only in the enabled export', () => {
    for (const marker of SURFACE_MARKERS) {
      expect(text(on)).toContain(marker)
      expect(text(off)).not.toContain(marker)
    }
  })
  it('ships reachable worker bytes in the enabled export', () => {
    for (const marker of ENGINE_MARKERS) {
      // Inspect the original emitted Next worker URL.
      const files = [...on.assets].filter(([file, code]) => file.startsWith('_next/') && code.includes(marker)).map(([file]) => file)
      expect(files.length).toBeGreaterThan(0)
      for (const file of files) expect(on.reachable.has(file)).toBe(true)
    }
  })
  it('cannot reach any worker bytes from the disabled export', () => {
    // An absent worker is fine; if the compiler emits an orphan it must stay unreachable.
    for (const marker of ENGINE_MARKERS) {
      for (const [file, code] of off.assets) if (code.includes(marker)) expect(off.reachable.has(file)).toBe(false)
    }
  })
  it('keeps the app positive control in both exports', () => {
    expect(text(on)).toContain('tc-rail-body')
    expect(text(off)).toContain('tc-rail-body')
  })
})
