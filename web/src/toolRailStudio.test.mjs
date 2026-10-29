import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { it } from 'node:test'
import { parse } from '@babel/parser'
import esbuild from 'esbuild'

const source = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })

function find(predicate, root = tree) {
  const found = []
  function walk(node) {
    if (!node || typeof node !== 'object') return
    if (predicate(node)) found.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value.type === 'string') walk(value)
    }
  }
  walk(root)
  return found
}

const calls = (node, name) => find(item => item.type === 'CallExpression' && item.callee.name === name, node)
const references = (node, name) => find(item => item.type === 'Identifier' && item.name === name, node)
const property = (node, name) => node.properties.find(item => item.key?.name === name)?.value
function evaluate(node, env) {
  return Function(...Object.keys(env), `return (${source.slice(node.start, node.end)})`)(...Object.values(env))
}

it('opening the tool rail reveals its Studio host only in the Studio shell', () => {
  const declarations = find(node => node.type === 'VariableDeclarator' && node.id.name === 'openToolRail')
  assert.equal(declarations.length, 1)
  const init = declarations[0].init
  assert.equal(init.callee.name, 'useCallback')
  const callback = init.arguments[0]
  const navCalls = calls(callback, 'setNavExpanded')
  assert.equal(navCalls.length, 1)
  assert.equal(navCalls[0].arguments[0].value, true)
  const panelCalls = calls(callback, 'setBrowserPanel')
  assert.equal(panelCalls.length, 1)
  assert.equal(panelCalls[0].arguments[0].value, 'studio')
  const guards = find(node => node.type === 'IfStatement' && node.test.name === 'studioShell', callback)
  assert.equal(guards.length, 1)
  assert.deepEqual(calls(guards[0].consequent, 'setBrowserPanel'), panelCalls)
  for (const studioShell of [false, true]) {
    const events = []
    evaluate(callback, {
      studioShell,
      setNavExpanded: value => events.push(['nav', value]),
      setBrowserPanel: value => events.push(['panel', value]),
    })()
    assert.deepEqual(events, studioShell ? [['nav', true], ['panel', 'studio']] : [['nav', true]])
  }
})

it('family openers use the shared tool rail opener except in the project catalog pane', () => {
  const handlers = find(node => (node.type === 'ObjectProperty' && node.key?.name === 'onOpenFamily')
    || (node.type === 'JSXAttribute' && node.name?.name === 'onOpenFamily'))
  assert.ok(handlers.length >= 4)
  let catalogHandlers = 0
  for (const handler of handlers) {
    const value = handler.type === 'JSXAttribute' ? handler.value.expression : handler.value
    const opensCatalog = calls(value, 'setProjectPane').some(call => call.arguments[0]?.value === 'catalog')
    if (opensCatalog) {
      catalogHandlers += 1
      assert.equal(calls(value, 'openToolRail').length, 0)
      const navCalls = calls(value, 'setNavExpanded')
      assert.equal(navCalls.length, 1)
      assert.equal(navCalls[0].arguments[0].value, true)
    } else {
      assert.equal(calls(value, 'openToolRail').length, 1)
      assert.equal(calls(value, 'setNavExpanded').length, 0)
    }
  }
  assert.equal(catalogHandlers, 1)
})

it('each Tool rail quick action reopens a hidden Studio host and closes only an expanded drawer', () => {
  const actions = find(node => node.type === 'ObjectExpression' && property(node, 'dataTool')?.value === 'rail-expand')
  assert.ok(actions.length > 0)
  for (const action of actions) {
    const expanded = property(action, 'expanded')
    const onClick = property(action, 'onClick')
    assert.ok(references(expanded, 'browserPanel').length)
    assert.ok(calls(onClick, 'openToolRail').length)
    for (const studioShell of [false, true]) {
      for (const drawerViewport of [false, true]) {
        for (const browserPanel of ['studio', 'assets']) {
          for (const studioDrawer of ['none', 'nav', 'history']) {
            for (const navExpanded of [false, true]) {
              const events = []
              const env = {
                studioShell, drawerViewport, browserPanel, studioDrawer, navExpanded,
                openToolRail: () => events.push(['open']),
                toggleStudioDrawer: value => events.push(['toggle', value]),
                setNavExpanded: value => events.push(['nav', value]),
              }
              const visible = (!studioShell || browserPanel === 'studio')
                && (drawerViewport ? studioDrawer === 'nav' : navExpanded)
              assert.equal(evaluate(expanded, env), visible)
              evaluate(onClick, env)()
              const expected = studioShell
                ? drawerViewport && visible ? ['toggle', 'nav'] : ['open']
                : drawerViewport ? ['toggle', 'nav'] : ['nav', property(action, 'title') ? true : !navExpanded]
              assert.deepEqual(events, [expected], JSON.stringify({ studioShell, drawerViewport, browserPanel, studioDrawer, navExpanded }))
            }
          }
        }
      }
    }
  }
})

it('App transforms and retains the executable tool rail callback', () => {
  const { code } = esbuild.transformSync(source, { loader: 'jsx', format: 'esm' })
  assert.match(code, /\bopenToolRail\b/)
  const transformed = parse(code, { sourceType: 'module' })
  assert.equal(find(node => node.type === 'VariableDeclarator' && node.id.name === 'openToolRail', transformed).length, 1)
})
