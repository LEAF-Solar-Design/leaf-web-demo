// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const sheets = ['../styles.css', './landing.css', '../campaigns/campaigns.css']
  .map((file) => [file, stripComments(readFileSync(new URL(file, import.meta.url), 'utf8'))])
const fontValues = (css) => [...css.matchAll(/(?:^|[;{}])\s*font\s*:\s*([^;}]+)/gi)]
  .map(([, value]) => value.trim())
const keyword = /^(?:inherit|initial|unset|revert|revert-layer|caption|icon|menu|message-box|small-caption|status-bar)$/i
const heroFont = "600 clamp(30px, 5vw, 58px)/1.02 'Inter Tight', sans-serif"

describe('font shorthand size tokens', () => {
  for (const [file, css] of sheets) {
    it(`${file} uses size tokens except for the single fluid landing hero`, () => {
      let declarationCount = 0
      let heroCount = 0
      for (const [, selector, body] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        for (const value of fontValues(body)) {
          declarationCount += 1
          if (keyword.test(value)) continue
          if (file === './landing.css' && selector.trim() === '.tc-product-frame h1' && value === heroFont) {
            heroCount += 1
            continue
          }
          // Skip optional style, variant, weight and stretch before the size.
          // Only inspect the size position; lengths after / are line-heights.
          const sizeAndFamily = value.replace(/^(?:(?:normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]\d{0,2}|(?:ultra-|extra-|semi-)?(?:condensed|expanded))\s+)*/i, '')
          expect(sizeAndFamily, `${file} ${selector.trim()} font: ${value}`)
            .toMatch(/^var\(\s*--leaf-size-[a-z0-9-]+\s*\)(?=\s|\/)/i)
        }
      }
      expect(declarationCount, file).toBeGreaterThan(0)
      expect(declarationCount, `${file} covers every font declaration`).toBe(fontValues(css).length)
      expect(heroCount, file).toBe(file === './landing.css' ? 1 : 0)
    })
  }
})
