import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')

function fontSizes(css) {
  const sizes = []
  for (const [, property, value] of css.matchAll(/(?:^|[;{}])\s*(font-size|font)\s*:\s*([^;}]+)/g)) {
    if (property === 'font-size') {
      sizes.push(value.trim())
      continue
    }
    // Ignore weight/style tokens and stop before the optional line height.
    const tokens = value.split('/')[0].match(/var\([^)]*\)|[^\s]+/g) || []
    const size = tokens.find((token) => /^(?:var\(|-?(?:\d*\.)?\d+(?:[a-z]+|%)$|(?:xx?-)?(?:small|large)$|medium$|smaller$|larger$)/i.test(token))
    if (size) sizes.push(size)
  }
  return sizes
}

describe('shell text minimum (L9)', () => {
  for (const [file, minimumCaptions] of [['studioShell.css', 15], ['cockpit.css', 45]]) {
    it(`${file} uses no pixel font size below 12`, () => {
      const css = stripComments(read(`./${file}`))
      const sizes = fontSizes(css)
      expect(sizes.length, file).toBeGreaterThan(0)
      const tooSmall = sizes.filter((size) => /^-?(?:\d*\.)?\d+px$/i.test(size) && parseFloat(size) < 12)
      expect(tooSmall, file).toEqual([])
      expect((css.match(/var\(--leaf-size-caption\)/g) || []).length, file).toBeGreaterThanOrEqual(minimumCaptions)
    })
  }

  it('the standard caption token is 12 px', () => {
    const css = stripComments(read('../standard/leaf-standard.css'))
    expect(css).toMatch(/--leaf-size-caption\s*:\s*12px\s*;/)
  })

  it('system regions wrap whole segments into hidden rows (L1)', () => {
    for (const file of ['studioShell.css', 'cockpit.css']) {
      const css = stripComments(read(`./${file}`))
      const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
        .filter(([, selector]) => selector.trim().endsWith('.foot-region-system'))
      expect(rules, file).toHaveLength(1)
      const body = rules[0][2]
      expect(body, file).toMatch(/justify-content\s*:\s*flex-end\s*;/)
      expect(body, file).toMatch(/overflow\s*:\s*hidden\s*;/)
      expect(body, file).toMatch(/flex-wrap\s*:\s*wrap\s*;/)
      expect(body, file).toMatch(/align-content\s*:\s*flex-start\s*;/)
    }
  })
})
