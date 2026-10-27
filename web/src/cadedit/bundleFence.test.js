import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'
import { bundleGraph } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'

const SURFACE_MARKERS = ['cad-edit-workbench', 'cad-edit-entity-list']
// Shared client error maps contain generic reason codes even with CAD off.
// The protocol/body pair identifies the actual compiled-engine worker.
const isEngineWorker = code => code.includes('engine_unavailable: the compiled CAD engine') && code.includes('documentLoaded')
const text = graph => [...graph.assets].filter(([file]) => file.endsWith('.js')).map(([, code]) => code).join('\n')
describe('cad_edit build fence', () => {
  let on
  let off
  beforeAll(async () => {
    const enabled = await nextFenceDirectory('enabled')
    on = bundleGraph(enabled)
    execFileSync(process.execPath, [resolve('scripts/check_next_worker.mjs'), enabled], { encoding: 'utf8' })
    console.log(execFileSync(process.execPath, [resolve('scripts/check_bundle_budget.mjs'), '--dist', enabled, '--measure-next'], { encoding: 'utf8' }).trim())
    off = bundleGraph(await nextFenceDirectory('disabled'))
  }, NEXT_FENCE_TIMEOUT)
  it('ships the editing surface only in the enabled export', () => {
    for (const marker of SURFACE_MARKERS) {
      expect(text(on)).toContain(marker)
      expect(text(off)).not.toContain(marker)
    }
  })
  it('ships reachable worker bytes in the enabled export', () => {
    const files = [...on.assets].filter(([file, code]) => file.startsWith('_next/') && isEngineWorker(code)).map(([file]) => file)
    expect(files.length).toBeGreaterThan(0)
    for (const file of files) expect(on.reachable.has(file)).toBe(true)
  })
  it('cannot reach any worker bytes from the disabled export', () => {
    for (const [file, code] of off.assets) if (isEngineWorker(code)) expect(off.reachable.has(file)).toBe(false)
  })
  it('keeps the app positive control in both exports', () => {
    expect(text(on)).toContain('tc-rail-body')
    expect(text(off)).toContain('tc-rail-body')
  })
})
