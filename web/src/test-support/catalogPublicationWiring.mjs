import assert from 'node:assert/strict'
import { parse } from '@babel/parser'
import { familiesForSurface } from '../lib/surfaceRails.js'

function nodes(root, predicate) {
  const found = []
  function visit(value) {
    if (!value || typeof value !== 'object') return
    if (predicate(value)) found.push(value)
    for (const child of Object.values(value)) {
      if (Array.isArray(child)) child.forEach(visit)
      else if (child && typeof child === 'object') visit(child)
    }
  }
  visit(root)
  return found
}

const names = (id) => !id ? [] : id.type === 'Identifier' ? [id.name]
  : id.type === 'ObjectPattern' ? id.properties.flatMap((property) => names(property.value || property.argument))
    : id.type === 'ArrayPattern' ? id.elements.flatMap(names)
      : id.type === 'AssignmentPattern' ? names(id.left) : names(id.argument)
const evaluate = (code, scope) => Function(...Object.keys(scope), `return (${code})`)(...Object.values(scope))

// A publication prop is a bare reference: a name, or a plain dotted path under one. A condition, a
// call, an optional or computed access is none of those, so no fixture value can hide a branch.
const bareReference = (node) => node?.type === 'Identifier'
  || (node?.type === 'MemberExpression' && !node.computed && node.property.type === 'Identifier'
    && bareReference(node.object))
// This helper reads each name from its one declaration and never runs the statements after it, so a
// later write would go unseen. `rootName` is the name a dotted path starts at; `written` lists every
// name an assignment target writes to: a name, the root of a member path, each part of a pattern.
const rootName = (node) => node?.type === 'Identifier' ? node.name
  : node?.type === 'MemberExpression' ? rootName(node.object) : null
const written = (node) => !node ? [] : node.type === 'Identifier' ? [node.name]
  : node.type === 'MemberExpression' ? [rootName(node)].filter(Boolean)
    : node.type === 'ObjectPattern' ? node.properties.flatMap((property) => written(property.value || property.argument))
      : node.type === 'ArrayPattern' ? node.elements.flatMap(written)
        : node.type === 'AssignmentPattern' ? written(node.left)
          : node.type === 'RestElement' ? written(node.argument) : []
const isWrite = (node) => node.type === 'AssignmentExpression' || node.type === 'UpdateExpression'
  || (node.type === 'UnaryExpression' && node.operator === 'delete')
  || ((node.type === 'ForInStatement' || node.type === 'ForOfStatement') && node.left.type !== 'VariableDeclaration')
// What may sit between a shell component and its panel: the component's own body and return, JSX,
// and a plain condition. A nested function would give the props another scope than the one this
// helper evaluates them in; a call would receive the element as an argument instead of mounting it.
const MOUNT_PATH = new Set(['FunctionDeclaration', 'BlockStatement', 'ReturnStatement', 'JSXElement',
  'JSXFragment', 'JSXExpressionContainer', 'LogicalExpression', 'ConditionalExpression'])
function pathTo(node, target, trail = []) {
  if (!node || typeof node !== 'object') return null
  if (node === target) return trail
  const next = typeof node.type === 'string' ? [...trail, node] : trail
  for (const child of Object.values(node)) {
    const found = Array.isArray(child)
      ? child.reduce((hit, item) => hit || pathTo(item, target, next), null)
      : pathTo(child, target, next)
    if (found) return found
  }
  return null
}

export function shellWiring(source, shell, owner, extra = {}) {
  assert.ok(shell === 'studio' || shell === 'toolcast')
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const text = (node) => source.slice(node.start, node.end)
  const component = tree.program.body.find((node) => node.type === 'ExportDefaultDeclaration')?.declaration
  assert.equal(component?.type, 'FunctionDeclaration')
  assert.equal(component.id.name, shell === 'studio' ? 'App' : 'ToolCast')
  const imported = tree.program.body.filter((node) => node.type === 'ImportDeclaration'
    && node.specifiers.some((item) => item.local.name === 'useCatalogController'))
  assert.equal(imported.length, 1)
  assert.equal(imported[0].source.value, shell === 'studio'
    ? './controllers/catalog/useCatalogController.js' : '../controllers/catalog/useCatalogController.js')
  const calls = nodes(tree, (node) => node.type === 'CallExpression' && node.callee?.name === 'useCatalogController')
  assert.equal(calls.length, 1)
  const statements = component.body.body.filter((node) => node.type === 'VariableDeclaration')
  const declarations = statements.flatMap((node) => node.declarations)
  const kinds = new Map(statements.flatMap((node) => node.declarations.map((item) => [item, node.kind])))
  const constant = (node) => assert.equal(kinds.get(node), 'const',
    `${names(node.id).join(', ')} is declared const, so nothing after the declaration can replace it`)
  const ownerDeclaration = declarations.find((node) => node.init === calls[0])
  assert.ok(ownerDeclaration, 'the owner is an unconditional component declaration')
  const declaration = (name) => {
    const found = declarations.filter((node) => names(node.id).includes(name))
    assert.equal(found.length, 1, `one live declaration of ${name}`)
    return found[0]
  }
  const noop = () => {}
  let ownerCalls = 0
  const scope = {
    catalogServices: {}, catalogAdapters: {}, mock: false, entitlements: null, agentDisabled: false,
    transportMock: false, busy: false, jobRunning: false, platform: { isEntitled: () => true },
    useCatalogController: () => { ownerCalls++; return owner },
    useMemo: (read) => read(), familiesForSurface, studioGround: true, activeSurface: 'cad',
    agentSessionId: 'session-1', sessionId: 'session-1', agentTurns: [], turns: [],
    boardConversation: false, setProjectPane: noop, setClaudeOpen: noop, setRightView: noop,
    onAttachAgentJob: noop, refreshJobs: noop, closeStartForChange: noop, attachJob: noop,
    engineDirty: false, writeLocked: false,
    ...extra,
  }
  const bind = (node) => {
    constant(node)
    const value = evaluate(text(node.init), scope)
    const result = Function('value', `const ${text(node.id)} = value; return { ${names(node.id).join(', ')} }`)(value)
    Object.assign(scope, result)
  }
  bind(ownerDeclaration)
  assert.equal(ownerCalls, 1)
  if (shell === 'studio') {
    assert.equal(scope.catalogActions, owner.actions)
    assert.equal(scope.catalogController, owner.controller)
    assert.equal(scope.catalogState, owner.state)
    const actions = declaration('retryTools')
    assert.equal(text(actions.init), 'catalogActions')
    bind(actions)
    bind(declaration('tools'))
    bind(declaration('railFamilies'))
  } else {
    assert.equal(scope.catalog, owner)
    assert.equal(text(declaration('tools').init), 'catalog.state')
    bind(declaration('tools'))
  }
  const element = (name) => {
    const found = nodes(tree, (node) => node.type === 'JSXElement'
      && node.openingElement.name.type === 'JSXIdentifier' && node.openingElement.name.name === name)
    assert.equal(found.length, 1, `one live ${name}`)
    assert.ok(found[0].start > component.start && found[0].end < component.end)
    return found[0]
  }
  const attributes = (node) => node.openingElement.attributes
  const props = (name, selected) => {
    const attrs = attributes(element(name))
    assert.ok(attrs.every((attr) => attr.type === 'JSXAttribute'), 'no uninspected spread')
    const wanted = selected || attrs.map((attr) => attr.name.name)
    return Object.fromEntries(wanted.map((name) => {
      const matches = attrs.filter((attr) => attr.name.name === name)
      assert.equal(matches.length, 1, `one ${name} attribute`)
      const value = matches[0].value
      return [name, value === null ? true : value.type === 'JSXExpressionContainer'
        ? evaluate(text(value.expression), scope) : value.value]
    }))
  }
  const expression = (name, attribute) => {
    const matches = attributes(element(name)).filter((attr) => attr.name?.name === attribute)
    assert.equal(matches.length, 1, `one ${attribute} attribute`)
    return matches[0].value?.expression
  }
  const mountPath = (name) => pathTo(component, element(name)).map((node) => node.type)
  // The names an expression is read through, followed back declaration by declaration to the owner call.
  const chain = (expressions) => {
    const seen = new Set(), queue = expressions.map(rootName)
    while (queue.length) {
      const name = queue.pop()
      assert.ok(name, 'a publication prop starts at a name')
      if (seen.has(name)) continue
      seen.add(name)
      const found = declaration(name)
      constant(found)
      if (found.init === calls[0]) continue
      assert.ok(bareReference(found.init), `${name} is read straight from the owner`)
      queue.push(rootName(found.init))
    }
    return seen
  }
  // Every name any statement of the component writes to, by name: a shadowed local counts too.
  const writes = () => nodes(component, isWrite).flatMap((node) => written(node.left || node.argument))
  return { panel: props('ConversePanel'), props, element, text, scope, attributes, expression, mountPath, chain, writes }
}

export function assertPublicationBindings(source, shell, owner, extra = {}) {
  const wiring = shellWiring(source, shell, owner, extra)
  assert.equal(typeof wiring.panel.onCatalogChanged, 'function')
  assert.equal(typeof wiring.panel.consumeCatalogPublication, 'function')
  assert.equal(wiring.panel.onCatalogChanged, owner.actions.refreshCatalog)
  assert.equal(wiring.panel.consumeCatalogPublication, owner.controller.consumeCatalogPublication)
  for (const name of ['onCatalogChanged', 'consumeCatalogPublication']) {
    assert.ok(bareReference(wiring.expression('ConversePanel', name)),
      `${name} is a bare reference, never a condition, a call or a wrapper`)
  }
  const owned = wiring.chain(['onCatalogChanged', 'consumeCatalogPublication']
    .map((name) => wiring.expression('ConversePanel', name)))
  assert.deepEqual(wiring.writes().filter((name) => owned.has(name)), [],
    'no statement in the shell writes to a name the publication props are read through')
  const path = wiring.mountPath('ConversePanel')
  assert.equal(path[0], 'FunctionDeclaration')
  assert.deepEqual(path.slice(1).filter((type) => type === 'FunctionDeclaration' || !MOUNT_PATH.has(type)), [],
    'only JSX and plain conditions sit between the shell component and its panel')
  assert.equal(path.filter((type) => type === 'BlockStatement').length, 1,
    'the component body is the only block around the panel, so no inner block can rebind a name')
  const retries = wiring.props(shell === 'studio' ? 'NavRail' : 'CapabilityCatalog', ['onRetryCatalog', 'onRetryTools'])
  assert.equal(retries.onRetryCatalog, owner.actions.loadCatalog)
  assert.equal(retries.onRetryTools, owner.actions.retryTools)
  return wiring
}

export function changePublicationProp(source, name, expression = null) {
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const panels = nodes(tree, (node) => node.type === 'JSXElement' && node.openingElement.name.name === 'ConversePanel')
  assert.equal(panels.length, 1)
  const attrs = panels[0].openingElement.attributes.filter((attr) => attr.name?.name === name)
  assert.equal(attrs.length, 1)
  const attr = attrs[0]
  return source.slice(0, attr.start) + (expression === null ? '' : `${name}={${expression}}`) + source.slice(attr.end)
}

// The panel's own JSX, unchanged, placed inside other source text: `before` and `after` surround it.
export function wrapPublicationPanel(source, before, after) {
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const panels = nodes(tree, (node) => node.type === 'JSXElement' && node.openingElement.name.name === 'ConversePanel')
  assert.equal(panels.length, 1)
  const [panel] = panels
  return source.slice(0, panel.start) + before + source.slice(panel.start, panel.end) + after + source.slice(panel.end)
}

function shellComponent(source) {
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const component = tree.program.body.find((node) => node.type === 'ExportDefaultDeclaration')?.declaration
  assert.equal(component?.type, 'FunctionDeclaration')
  return component
}

// The component statement that returns the panel, unchanged, with `before` and `after` around it.
export function wrapPublicationReturn(source, before, after) {
  const component = shellComponent(source)
  const panels = nodes(component, (node) => node.type === 'JSXElement' && node.openingElement.name.name === 'ConversePanel')
  assert.equal(panels.length, 1)
  const found = component.body.body.filter((node) => node.start <= panels[0].start && node.end >= panels[0].end)
  assert.equal(found.length, 1)
  assert.equal(found[0].type, 'ReturnStatement')
  return source.slice(0, found[0].start) + before + source.slice(found[0].start, found[0].end) + after + source.slice(found[0].end)
}

// The component's own `const` declaration of `name`, redeclared with `let`.
export function relaxDeclaration(source, name) {
  const found = shellComponent(source).body.body.filter((node) => node.type === 'VariableDeclaration'
    && node.declarations.some((item) => names(item.id).includes(name)))
  assert.equal(found.length, 1)
  assert.equal(found[0].kind, 'const')
  assert.equal(source.slice(found[0].start, found[0].start + 5), 'const')
  return source.slice(0, found[0].start) + 'let' + source.slice(found[0].start + 5)
}
