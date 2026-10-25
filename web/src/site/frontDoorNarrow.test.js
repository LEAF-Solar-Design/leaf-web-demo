// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')

// Consume whole top-level blocks so media rules cannot satisfy base assertions.
const blocks = [...css.matchAll(/([^{}]+)\{((?:[^{}]|\{[^{}]*\})*)\}/g)]
const baseRule = (selector) => blocks.find((block) => block[1].trim() === selector)?.[2]
const declaration = (body, property) => body?.match(
  new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+);`),
)?.[1].trim()

describe('front door narrow tablet layout', () => {
  it('keeps the base sheet label on one line', () => {
    expect(declaration(baseRule('.lp-sheetno'), 'white-space')).toBe('nowrap')
  })

  it('fits the statement in the same tablet band that shrinks the top slot', () => {
    const tablet = blocks.find((block) => (
      /^@media\s*\(\s*max-width\s*:\s*1023px\s*\)$/.test(block[1].trim())
      && declaration(block[2].match(/(?:^|\})\s*\.lp-top-slot\s*\{([^{}]*)\}/)?.[1], 'min-width') === '0'
    ))?.[2]
    expect(tablet).toBeDefined()
    const statement = tablet?.match(/(?:^|\})\s*\.lp-stmt\s*\{([^{}]*)\}/)?.[1]
    expect(declaration(statement, 'width')).toBe('min(560px, calc(100vw - 120px))')
  })

  it('preserves the wide statement width and left offset', () => {
    const statement = baseRule('.lp-stmt')
    expect(declaration(statement, 'width')).toBe('560px')
    expect(declaration(statement, 'left')).toBe('60px')
  })
})
