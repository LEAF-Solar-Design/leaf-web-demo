// @vitest-environment node
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

describe('front door text minimum (L9)', () => {
  it('keeps caption-sized product tab subtitles within the product nav height', () => {
    const css = stripComments(read('./landing.css'))
    const subtitle = css.match(/^\.tc-product-tabs small\s*\{([^{}]*)\}/m)?.[1]
    const button = css.match(/^\.tc-product-tabs button\s*\{([^{}]*)\}/m)?.[1]
    expect(subtitle).toMatch(/(?:^|;)\s*font-size:\s*var\(--leaf-size-caption\)\s*;/)
    expect(subtitle).toMatch(/(?:^|;)\s*line-height:\s*1\s*;/)
    expect(button).toMatch(/(?:^|;)\s*padding:\s*4px 9px\s*;/)
  })

  it('landing.css uses no pixel font size below 12', () => {
    const css = stripComments(read('./landing.css'))
    const sizes = fontSizes(css)
    expect(sizes.length).toBeGreaterThan(0)
    const tooSmall = sizes.filter((size) => /^-?(?:\d*\.)?\d+px$/i.test(size) && parseFloat(size) < 12)
    expect(tooSmall).toEqual([])
    expect((css.match(/var\(--leaf-size-caption\)/g) || []).length).toBeGreaterThanOrEqual(79)
  })
})
