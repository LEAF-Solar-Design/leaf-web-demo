import { readFileSync } from 'node:fs'
import { URL } from 'node:url'
import { describe, it, expect } from 'vitest'

const readCss = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const css = readCss('../styles.css')

// Match a size before any optional /line-height. Bare numeric weights are
// excluded; functions stay whole so a caption token is also a single size.
const shorthandSize = /(?:^|\s)([+-]?(?:\d*\.)?\d+(?:[a-z]+|%)|var\([^)]*\)|(?:xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|smaller|larger))(?=\s|\/|$)/i

describe('base stylesheet text minimum (L9)', () => {
  it('has no pixel font size below 12 in longhands or shorthands', () => {
    const sizes = [...css.matchAll(/(?:^|[;{])\s*(font-size|font)\s*:\s*([^;}]+)/gi)]
      .map(([, property, value]) => ({
        declaration: `${property}: ${value.trim()}`,
        size: property.toLowerCase() === 'font-size'
          ? value.replace(/\s*!important\s*$/i, '').trim()
          : value.match(shorthandSize)?.[1],
      }))
    expect(sizes.length).toBeGreaterThan(0)
    const undersized = sizes.filter(({ size }) =>
      /^[+-]?(?:\d*\.)?\d+px$/i.test(size ?? '') && parseFloat(size) < 12,
    )
    expect(undersized).toEqual([])
  })

  it('uses the caption token for at least 100 font sizes', () => {
    expect([...css.matchAll(/var\(--leaf-size-caption\)/g)].length).toBeGreaterThanOrEqual(100)
  })

  it('pins the standard caption token to 12px', () => {
    expect(readCss('../standard/leaf-standard.css')).toMatch(/--leaf-size-caption\s*:\s*12px\s*(?:;|})/)
  })
})
