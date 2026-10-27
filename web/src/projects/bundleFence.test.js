// Inspect the shared current-source Next exports from the flag matrix.
import { beforeAll, describe, expect, it } from 'vitest'
import { emittedJavaScript } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'

const MARKERS = ['projects-surface', 'membership-panel']
describe('lifecycle_ui build fence', () => {
  let onText
  let offText
  beforeAll(async () => {
    onText = [...emittedJavaScript(await nextFenceDirectory('enabled')).values()].join('\n')
    offText = [...emittedJavaScript(await nextFenceDirectory('disabled')).values()].join('\n')
  }, NEXT_FENCE_TIMEOUT)
  it('ships the lifecycle surface only in the enabled export', () => {
    for (const marker of MARKERS) {
      expect(onText).toContain(marker)
      expect(offText).not.toContain(marker)
    }
  })
  it('keeps the app positive control in both exports', () => {
    for (const text of [onText, offText]) expect(text).toContain('tc-rail-body')
  })
})
