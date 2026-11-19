// @vitest-environment node
import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'

const css = readFileSync(new URL('./drawingObjectList.css', import.meta.url), 'utf8')

it('uses the caption size token for the incomplete drawing warning', () => {
  const rule = css.match(/^\.drawing-incomplete-view\s*\{([^}]*)\}/m)?.[1]
  expect(rule).toContain('font-size: var(--leaf-size-caption);')
})

it('positions the warning above the Studio drawing canvas', () => {
  const selector = '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"]) .drawing-incomplete-view'
  const rule = css.split(selector)[1]?.match(/^\s*\{([^}]*)\}/)?.[1]
  expect(rule).toContain('position: fixed;')
  expect(rule).toContain('z-index: calc(var(--leaf-layer-base) + 7);')
  expect(rule).toContain('top: calc(var(--ck-canvas-top, 140px) + 8px);')
})
