// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const css = path => read(path).replace(/\/\*[\s\S]*?\*\//g, '')
const sources = [
  '../campaigns/campaigns.css',
  '../components/detailsDrawerSheet.css',
  '../demo/coach.css',
  '../lab/lab.css',
  '../lab/widgets/widgets.css',
  './sheets.css',
  '../structural.css',
  '../styles.css',
]

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

describe('standard media breakpoints', () => {
  it.each(sources)('%s uses only the standard width conditions', path => {
    const blocks = mediaBlocks(css(path))
    expect(blocks.length).toBeGreaterThan(0)
    for (const { prelude } of blocks) {
      const otherConditions = prelude.replace(
        /\(\s*(?:max-width\s*:\s*(?:599|1023|1439)px|min-width\s*:\s*(?:600|1024|1440)px)\s*\)/gi,
        '',
      )
      expect(otherConditions, `${path}: ${prelude}`).not.toMatch(/\bwidth\b/i)
    }
  })

  it('keeps the coach and tour bar on the same wide edge', () => {
    const coach = mediaBlocks(css('../demo/coach.css'))
    const landing = mediaBlocks(css('./landing.css'))
    expect(coach.some(block => block.prelude === '(max-width: 1439px)' && /\.coach-root\s*\{/.test(block.body))).toBe(true)
    expect(landing.some(block => block.prelude === '(max-width: 1439px)' && /\.tc-bar-wrap\s*\{/.test(block.body))).toBe(true)
    expect(coach.some(block => block.prelude === '(min-width: 1440px)' && /\.coach-card\s*\{/.test(block.body))).toBe(true)
  })

  it('keeps the DetailsDrawer phone query aligned with its first CSS media block', () => {
    const drawer = read('../components/DetailsDrawer.jsx')
    expect(drawer).toContain("'(max-width: 599px)'")
    expect(drawer).not.toContain('639px')
    expect(mediaBlocks(css('../components/detailsDrawerSheet.css'))[0].prelude).toBe('(max-width: 599px)')
  })
})
