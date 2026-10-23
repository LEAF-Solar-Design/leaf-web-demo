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

describe('known guest authenticated reads', () => {
  it('declares the pre-answer guest signal before mounting the catalog controller', () => {
    const guest = one((node) => node.type === 'VariableDeclarator' && node.id.name === 'knownGuest')
    expect(text(guest.node.init)).toBe('consoleKnownGuest({ mock, authConfigured, isSignedIn, authCallback: isAuthRedirectCallback() })')
    const catalog = one((node) => node.type === 'CallExpression' && node.callee.name === 'useCatalogController')
    expect(guest.node.start).toBeLessThan(catalog.node.start)
  })

  it('pauses the catalog context for a known guest', () => {
    const catalog = one((node) => node.type === 'CallExpression' && node.callee.name === 'useCatalogController')
    const context = catalog.node.arguments[0].properties.find((property) => property.key?.name === 'context')
    expect(context.value.type).toBe('ObjectExpression')
    const paused = context.value.properties.filter((property) => property.key?.name === 'guestReadsPaused')
    expect(paused).toHaveLength(1)
    expect(text(paused[0])).toBe('guestReadsPaused: knownGuest')
  })

  it('gives checkout a null drawing scope for a known guest', () => {
    const checkout = one((node) => node.type === 'CallExpression' && node.callee.name === 'useCheckoutController')
    const drawingId = checkout.node.arguments[0].properties.find((property) => property.key?.name === 'drawingId')
    expect(text(drawingId.value)).toMatch(/^knownGuest \? null : checkoutScopeDrawingId\(/)
  })

  it('pauses platform authenticated reads for a known guest', () => {
    const platform = one((node) => node.type === 'CallExpression' && node.callee.name === 'usePlatformTrustController')
    expect(platform.node.arguments).toHaveLength(1)
    const options = platform.node.arguments[0]
    expect(options.type).toBe('ObjectExpression')
    expect(options.properties.map(text)).toEqual(['mock', 'guestReadsPaused: knownGuest'])
  })

  it('stops the quota re-poll for a known guest', () => {
    const poll = one((node) => node.type === 'CallExpression' && node.callee.name === 'useEffect'
      && text(node.arguments[0].body).includes('setInterval(loadUsage, 60_000)'))
    const first = poll.node.arguments[0].body.body[0]
    expect(first.type).toBe('IfStatement')
    expect(text(first.test)).toBe('mock || knownGuest || (!quotaError && !runQuotaError)')
    expect(poll.node.arguments[1].elements.map(text)).toEqual(['mock', 'knownGuest', 'quotaError', 'runQuotaError', 'loadUsage'])
  })
})
