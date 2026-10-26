// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./studioShell.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

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
    return { prelude: match[1].trim(), body: source.slice(start, end - 1) }
  })
}

const rulesAt = width => mediaBlocks(css)
  .filter(block => block.prelude === `(min-width: ${width}px)`)
  .flatMap(block => [...block.body.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(match => ({ selector: match[1].trim(), body: match[2] })))
const declaration = (body, property) => body.match(
  new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+);`),
)?.[1].trim()
const iosNav = '.studio-shell .app[data-studio-shell="cockpit"][data-surface="ios"] .tc-product-nav'
const pinnedNav = '.studio-shell .app[data-studio-shell="cockpit"][data-surface] .tc-product-nav'

describe('iOS pinned product nav anchor', () => {
  it('anchors iOS after Ship from 1440px up', () => {
    expect(rulesAt(1440).some(rule => rule.selector === iosNav
      && declaration(rule.body, 'left') === '440px')).toBe(true)
  })

  it('preserves the shared pinned rule from 1024px up', () => {
    expect(rulesAt(1024).some(rule => rule.selector === pinnedNav
      && declaration(rule.body, 'position') === 'fixed'
      && declaration(rule.body, 'left') === 'max(600px, 45%)')).toBe(true)
  })

  it('keeps iOS out of the relative own row from 1440px up', () => {
    expect(rulesAt(1440).filter(rule => rule.selector.includes('[data-surface="ios"] .tc-product-nav')
      && declaration(rule.body, 'position') === 'relative')).toEqual([])
  })
})
