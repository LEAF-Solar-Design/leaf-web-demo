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

const topLevel = blocks(css)
const desktop = topLevel.filter(block => block.prelude === '@media (min-width: 1024px)')
const cases = [
  {
    name: 'row tools',
    selector: '.studio-shell .app[data-studio-shell="cockpit"][data-surface] .ribbon-tool[data-size="row"]',
    min: '104px',
    max: '180px',
  },
  {
    name: 'profile command rows',
    selector: '.studio-shell .app[data-studio-shell="cockpit"] .studio-chrome-host[data-ribbon-context="workspace"] .ribbon-tool',
    min: '168px',
    max: '220px',
  },
]

describe('desktop ribbon row label fit', () => {
  for (const { name, selector, min, max } of cases) {
    it(`${name} grow within bounds at desktop widths`, () => {
      const rules = desktop.flatMap(block => blocks(block.body))
        .filter(rule => rule.prelude === selector)
      expect(rules).toHaveLength(1)
      expect(declaration(rules[0].body, 'width')).toBe('auto')
      expect(declaration(rules[0].body, 'min-width')).toBe(min)
      expect(declaration(rules[0].body, 'max-width')).toBe(max)
    })

    it(`${name} preserve their base width outside media blocks`, () => {
      const rules = topLevel.filter(rule => rule.prelude === selector)
      expect(rules).toHaveLength(1)
      expect(declaration(rules[0].body, 'width')).toBe(min)
    })
  }

  it('keeps the desktop label-fit block last in the file', () => {
    const last = topLevel.at(-1)
    expect(last.prelude).toBe('@media (min-width: 1024px)')
    expect(blocks(last.body).map(rule => rule.prelude)).toEqual(cases.map(entry => entry.selector))
    expect(css.trimEnd().endsWith(`{${last.body}}`)).toBe(true)
  })
})
