// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./studioShell.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

const balancedBody = (source, openingBrace) => {
  let depth = 0
  for (let index = openingBrace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(openingBrace + 1, index)
  }
  throw new Error('Unbalanced CSS block')
}

const mediaBlocks = (query) => {
  const marker = `@media ${query}`
  const blocks = []
  let depth = 0
  for (let index = 0; index < css.length; index += 1) {
    if (css[index] === '{') depth += 1
    if (css[index] === '}') depth -= 1
    if (depth === 0 && css.startsWith(marker, index)) {
      const openingBrace = css.indexOf('{', index + marker.length)
      const body = balancedBody(css, openingBrace)
      blocks.push(body)
      index = openingBrace + body.length + 1
    }
  }
  return blocks
}

const ruleBodies = (block, selector) => {
  const bodies = []
  let offset = 0
  while ((offset = block.indexOf(selector, offset)) !== -1) {
    const openingBrace = block.indexOf('{', offset + selector.length)
    bodies.push(balancedBody(block, openingBrace))
    offset = openingBrace + bodies.at(-1).length + 2
  }
  return bodies
}

const ruleBody = (block, selector) => {
  const bodies = ruleBodies(block, selector)
  expect(bodies.length).toBeGreaterThan(0)
  return bodies[0]
}

const declaration = (body, property, value) => {
  expect(body).toMatch(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*${value}\\s*(?:;|$)`))
}

const desktopTabSelector = '.studio-shell .app[data-studio-shell="cockpit"][data-surface] .tc-product-tabs [role="tab"]'
const desktopStatusSelector = `${desktopTabSelector} small`
const ownRowStatusSelector = '.studio-shell .app[data-studio-shell="cockpit"][data-surface]:not([data-surface="cad"]):not([data-surface="ios"]) .tc-product-tabs [role="tab"] small'

describe('product tab status layout', () => {
  const desktopBlock = mediaBlocks('(min-width: 1024px)')
    .find(block => block.includes(desktopTabSelector) && /height\s*:\s*22px/.test(block))
  const ownRowBlock = mediaBlocks('(min-width: 1440px)')
    .find(block => block.includes('.tc-product-tabs { align-self: flex-start; }'))

  it('keeps desktop tabs on one 22px row', () => {
    expect(desktopBlock).toBeDefined()
    const tab = ruleBody(desktopBlock, desktopTabSelector)
    declaration(tab, 'flex-direction', 'row')
    declaration(tab, 'height', '22px')
    declaration(tab, 'gap', '5px')
  })

  it('visually hides desktop status without removing it from the accessible name', () => {
    const status = ruleBodies(desktopBlock, desktopStatusSelector)
      .find(body => /position\s*:\s*absolute/.test(body))
    expect(status).toBeDefined()
    declaration(status, 'position', 'absolute')
    declaration(status, 'width', '1px')
    declaration(status, 'height', '1px')
    declaration(status, 'overflow', 'hidden')
    declaration(status, 'clip-path', 'inset\\(50%\\)')
    declaration(status, 'white-space', 'nowrap')
    expect(status).not.toMatch(/(?:^|;)\s*display\s*:\s*none\s*(?:;|$)/)
  })

  it('shows status inline in the 1440px own row', () => {
    expect(ownRowBlock).toBeDefined()
    const status = ruleBody(ownRowBlock, ownRowStatusSelector)
    declaration(status, 'position', 'static')
    declaration(status, 'width', 'auto')
    declaration(status, 'height', 'auto')
    declaration(status, 'overflow', 'visible')
    declaration(status, 'clip-path', 'none')
  })

  it('keeps phone and tablet status display disabled', () => {
    const narrowBlock = mediaBlocks('(max-width: 1023px)')
      .find(block => block.includes('.tc-product-tabs [role="tab"] small'))
    expect(narrowBlock).toBeDefined()
    const status = ruleBody(narrowBlock, '.studio-shell .app[data-studio-shell="cockpit"] .tc-product-tabs [role="tab"] small')
    declaration(status, 'display', 'none')
  })
})
