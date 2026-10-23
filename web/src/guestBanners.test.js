// @vitest-environment node
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const entries = []
function walk(node, ancestors = []) {
  if (!node || typeof node.type !== 'string') return
  entries.push({ node, ancestors })
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach((child) => walk(child, [...ancestors, node]))
    else if (value && typeof value.type === 'string') walk(value, [...ancestors, node])
  }
}
walk(tree)
const text = (node) => source.slice(node.start, node.end)
function one(predicate) {
  const matches = entries.filter(({ node }) => predicate(node))
  expect(matches).toHaveLength(1)
  return matches[0]
}
const opening = (name) => one((node) => node.type === 'JSXOpeningElement' && node.name.name === name)
function prop(node, name) {
  const matches = node.attributes.filter((attribute) => attribute.type === 'JSXAttribute' && attribute.name.name === name)
  expect(matches).toHaveLength(1)
  return text(matches[0].value.expression)
}

describe('signed-out Studio banners', () => {
  it('derives the visible catalog error immediately after the canonical signed-out signal', () => {
    const signedOut = one((node) => node.type === 'VariableDeclarator' && node.id.name === 'signedOut')
    expect(text(signedOut.node.init)).toBe('consoleSignedOut({ mock, authRequired, authConfigured, isSignedIn })')
    const visible = one((node) => node.type === 'VariableDeclarator' && node.id.name === 'visibleCatalogErr')
    expect(text(visible.node.init)).toBe('signedOut ? null : catalogErr')
    const statements = signedOut.ancestors.at(-2).body
    expect(statements[statements.indexOf(signedOut.ancestors.at(-1)) + 1]).toBe(visible.ancestors.at(-1))
  })

  it('passes the visible catalog error to both shell surfaces and retains the ribbon condition', () => {
    expect(prop(opening('SurfaceFrame').node, 'catalogError')).toBe('visibleCatalogErr')
    expect(prop(opening('DraftingRibbon').node, 'catalogError')).toBe("navSpine || (phoneViewport && studioDrawer !== 'nav') ? visibleCatalogErr : null")
    expect(source).not.toContain('catalogError={catalogErr}')
  })

  it('keeps checkout controls inside the live signed-in wrapper', () => {
    const checkout = opening('CheckoutControls')
    const guards = checkout.ancestors.filter((node) => node.type === 'LogicalExpression'
      && node.operator === '&&' && text(node.left) === '!mock && !signedOut')
    expect(guards).toHaveLength(1)
    expect(guards[0].right.type).toBe('ConditionalExpression')
    expect(text(guards[0].right.test)).toBe('projectMode')
  })

  it('keeps the live Claude accounts chip mounted and passes the canonical signed-out signal', () => {
    const panel = opening('ClaudeAccountPanel')
    expect(prop(panel.node, 'signedOut')).toBe('signedOut')
    const guards = panel.ancestors.filter((node) => node.type === 'LogicalExpression' && node.operator === '&&')
    expect(guards.map((node) => text(node.left))).toEqual(['!mock'])
  })
})
