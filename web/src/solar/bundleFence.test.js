import { beforeAll, describe, expect, it } from 'vitest'
import { emittedJavaScript } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'

// Unit help also belongs to shared project settings, so it is not evidence
// that the gated Solar form or flow rail ships. These markers are surface-only.
const MARKERS = ['solar-settings-form', 'solar-flow-rail', 'solar-flow-select']
describe('Solar settings build fence', () => {
  let onText
  let offText
  beforeAll(async () => {
    onText = [...emittedJavaScript(await nextFenceDirectory('enabled')).values()].join('\n')
    offText = [...emittedJavaScript(await nextFenceDirectory('disabled')).values()].join('\n')
  }, NEXT_FENCE_TIMEOUT)
  it('ships the Solar form and rail only in the enabled export', () => {
    for (const marker of MARKERS) {
      expect(onText).toContain(marker)
      expect(offText).not.toContain(marker)
    }
  })
  it('keeps production app controls in both exports', () => {
    for (const text of [onText, offText]) {
      expect(text).toContain('solar-tool-form')
      expect(text).not.toContain('jsxDEV(')
      expect(text).not.toContain(process.cwd().replace(/\\/g, '/'))
    }
  })
})
