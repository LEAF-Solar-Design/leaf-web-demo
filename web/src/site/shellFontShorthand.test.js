// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const sheets = ['./cockpit.css', './studioShell.css', '../leafPlatform/leafPlatform.css']
  .map((file) => [file, stripComments(readFileSync(new URL(file, import.meta.url), 'utf8'))])
const fontValues = (css) => [...css.matchAll(/(?:^|[;{}])\s*font\s*:\s*([^;}]+)/gi)]
  .map(([, value]) => value.trim())
const keyword = /^(?:inherit|initial|unset|revert|revert-layer|caption|icon|menu|message-box|small-caption|status-bar)$/i

describe('shell font shorthand size tokens', () => {
  for (const [file, css] of sheets) {
    it(`${file} uses a leaf size token in every non-keyword font shorthand`, () => {
      const values = fontValues(css)
      expect(values.length, file).toBeGreaterThan(0)
      for (const value of values) {
        if (keyword.test(value)) continue
        // Skip optional style, variant, weight and stretch keywords before the size.
        // Check only the size position: lengths after / belong to the line-height.
        const sizeAndFamily = value.replace(/^(?:(?:normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]\d{0,2}|(?:ultra-|extra-|semi-)?(?:condensed|expanded))\s+)*/i, '')
        expect(sizeAndFamily, `${file} font: ${value}`)
          .toMatch(/^var\(\s*--leaf-size-[a-z0-9-]+\s*\)(?=\s|\/)/i)
      }
    })
  }

  it('.doc-tab-add keeps its /1 line-height', () => {
    const css = sheets.find(([file]) => file === './cockpit.css')[1]
    const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .filter(([, selector]) => selector.trim() === '.doc-tab-add')
    expect(rules).toHaveLength(1)
    const values = fontValues(rules[0][2])
    expect(values).toHaveLength(1)
    expect(values[0]).toMatch(/^400\s+var\(--leaf-size-body\)\s*\/\s*1\s+var\(--ck-font\)$/)
  })
})
