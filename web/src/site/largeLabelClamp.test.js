// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./studioShell.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

// Read balanced top-level blocks so media rules cannot satisfy a base-width pin.
function blocks(source) {
  const result = []
  let cursor = 0
  while (cursor < source.length) {
    const open = source.indexOf('{', cursor)
    if (open === -1) break
    let end = open + 1
    let depth = 1
    while (end < source.length && depth > 0) {
      if (source[end] === '{') depth++
      if (source[end] === '}') depth--
      end++
    }
    result.push({ prelude: source.slice(cursor, open).trim(), body: source.slice(open + 1, end - 1) })
    cursor = end
  }
  return result
}

const declaration = (body, property) => body.match(
  new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)(?:;|$)`),
)?.[1].trim()

const selector = '.studio-shell .app[data-studio-shell="cockpit"][data-surface] .ribbon-tool[data-size="large"] .ribbon-tool-label'
const topLevel = blocks(css)
const wide = topLevel.filter(block => block.prelude === '@media (min-width: 1440px)'
  && blocks(block.body).some(rule => rule.prelude === selector))

describe('wide desktop large ribbon label clamp', () => {
  it('clamps large labels to two lines with an ellipsis from 1440px', () => {
    expect(wide).toHaveLength(1)
    const rules = blocks(wide[0].body).filter(rule => rule.prelude === selector)
    expect(rules).toHaveLength(1)
    expect(declaration(rules[0].body, 'display')).toBe('-webkit-box')
    expect(declaration(rules[0].body, '-webkit-box-orient')).toBe('vertical')
    expect(declaration(rules[0].body, '-webkit-line-clamp')).toBe('2')
    expect(declaration(rules[0].body, 'line-clamp')).toBe('2')
  })

  it('preserves the base two-line height and hidden overflow outside media blocks', () => {
    const rules = topLevel.filter(rule => rule.prelude === selector)
    expect(rules).toHaveLength(1)
    expect(declaration(rules[0].body, 'display')).toBe('block')
    expect(declaration(rules[0].body, 'max-height')).toBe('26px')
    expect(declaration(rules[0].body, 'overflow')).toBe('hidden')
  })

  it('preserves unrestricted labels below 1440px', () => {
    const rules = topLevel.filter(block => block.prelude === '@media (max-width: 1439px)')
      .flatMap(block => blocks(block.body))
      .filter(rule => rule.prelude === selector)
    expect(rules).toHaveLength(1)
    expect(declaration(rules[0].body, 'max-height')).toBe('none')
    expect(declaration(rules[0].body, 'overflow')).toBe('visible')
  })

  it('keeps the wide clamp block immediately before the last block', () => {
    expect(wide).toHaveLength(1)
    expect(topLevel.at(-2)).toBe(wide[0])
  })
})
