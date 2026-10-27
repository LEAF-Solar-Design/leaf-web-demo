// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const readCss = path => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

// Track media ranges with balanced braces so nested rules cannot satisfy a base pin.
function mediaBlocks(source) {
  return [...source.matchAll(/@media\b([^{}]+)\{/g)].map(match => {
    const start = match.index + match[0].length
    let end = start
    let depth = 1
    while (end < source.length && depth > 0) {
      if (source[end] === '{') depth++
      if (source[end] === '}') depth--
      end++
    }
    return { prelude: match[1].trim(), start, end: end - 1 }
  })
}

function largeToolRules(source) {
  const media = mediaBlocks(source)
  return [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(match => match[1].trim().endsWith('.ribbon-tool[data-size="large"]'))
    .map(match => ({
      body: match[2],
      media: media.filter(block => match.index >= block.start && match.index < block.end),
    }))
}

const declaration = (body, property) => body.match(
  new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)(?:;|$)`),
)?.[1].trim()

describe('large ribbon tool width', () => {
  for (const path of ['./studioShell.css', './cockpit.css']) {
    const rules = largeToolRules(readCss(path))

    it(`${path} gives the base 78px-high large tool a 66px width`, () => {
      const base = rules.filter(rule => rule.media.length === 0
        && declaration(rule.body, 'height') === '78px')
      expect(base).toHaveLength(1)
      expect(declaration(base[0].body, 'width')).toBe('66px')
    })

    it(`${path} has no remaining 57px large tool width`, () => {
      for (const rule of rules) {
        expect(rule.body).not.toMatch(/(?:^|;)\s*width\s*:\s*57px\s*(?:!important\s*)?(?:;|$)/)
      }
    })
  }

  it('preserves the 76px studio shell width below 1440px', () => {
    const narrow = largeToolRules(readCss('./studioShell.css')).filter(rule =>
      rule.media.some(block => block.prelude === '(max-width: 1439px)'))
    expect(narrow).toHaveLength(1)
    expect(declaration(narrow[0].body, 'width')).toBe('76px')
  })
})
