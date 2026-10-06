// Guard against a declaration being swallowed by a comment.
//
// App.jsx carries a ~140-line commented-out legacy block
// ("Legacy inline catalog dispatch is disabled; useCatalogController owns
// it."). Inserting a hook just inside it is easy, silent, and fatal: the JSX
// still references the binding, so the component throws ReferenceError on its
// FIRST render — and `npm run build` passes, because commented-out code is
// still valid syntax. Unit tests over pure modules miss it too, since they
// never render App.
//
// esbuild strips comments, so "does this declaration survive the transform"
// is a direct, cheap answer. Verified to fail on the exact commit where the
// declaration sat inside that block.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import esbuild from 'esbuild'
import { parse as parseJs } from '@babel/parser'
import {
  prepareCatalogRunParams, createRunIntentState, createCatalogToolSnapshot,
  stageRunIntent, confirmRunIntent, dismissRunIntent,
} from './runIntent.js'
import { slashDecision, alternativeDecision } from './controllers/catalog/catalogRouting.js'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')

describe('S17 version-created toast Undo wiring', () => {
  const surfaces = [
    { name: 'App', source: appSource, completion: 'seatCompletedVersion', directCount: 3, undoHandler: 'undoCurrentVersion' },
    { name: 'ToolCast', source: readFileSync(new URL('./site/ToolCast.jsx', import.meta.url), 'utf8'), completion: 'onCompleteVersion', directCount: 3, undoHandler: 'undo' },
  ]
  function property(object, name) {
    return object?.type === 'ObjectExpression'
      ? object.properties.find((item) => csuKey(item) === name)?.value
      : undefined
  }
  function toastObjects(tree) {
    const found = []
    csuWalk(tree, (node) => {
      if (node.type !== 'CallExpression' || node.callee.type !== 'Identifier' || node.callee.name !== 'showToast') return
      csuWalk(node.arguments[0], (argument) => {
        if (property(argument, 'text')) found.push(argument)
      })
    })
    return found
  }
  function textPattern(object) {
    const text = property(object, 'text')
    if (text?.type === 'StringLiteral') return text.value
    if (text?.type === 'TemplateLiteral') return text.quasis.map((part) => part.value.cooked).join('#')
    return ''
  }
  function assertUndo(object) {
    const action = property(object, 'action')
    assert.equal(action?.type, 'ObjectExpression')
    assert.equal(action.properties.length, 3)
    assert.ok(action.properties.every((item) => item.type === 'ObjectProperty'))
    assert.equal(property(action, 'label')?.value, 'Undo')
    assert.equal(property(action, 'undo')?.type, 'BooleanLiteral')
    assert.equal(property(action, 'undo')?.value, true)
    assert.equal(property(action, 'onClick')?.type, 'Identifier')
    assert.equal(property(action, 'onClick')?.name, 'onUndo')
  }

  for (const surface of surfaces) {
    const tree = parseJs(surface.source, { sourceType: 'module', plugins: ['jsx'] })
    const declarations = new Map()
    csuWalk(tree, (node) => {
      if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier') declarations.set(node.id.name, node)
    })
    const completion = declarations.get(surface.completion)
    const seat = surface.name === 'App' ? declarations.get('seatVersion') : null
    const versionToasts = toastObjects(tree).filter((object) => /^Version .+ created$/.test(textPattern(object)))
    const seatedToasts = seat ? toastObjects(seat.init.arguments[0]) : []

    it(`${surface.name}: every live version-created toast has Undo wired to onUndo`, () => {
      assert.equal(versionToasts.length, surface.directCount, 'all completion branches must announce the version')
      for (const object of versionToasts) assertUndo(object)
      assert.ok(versionToasts.every((object) => object.start > completion.start && object.end < completion.end))
      assert.ok(completion.init.arguments[1].elements.some((node) => node.name === 'onUndo'))
      if (seat) {
        assert.equal(seatedToasts.length, 1, 'the successful App seat also announces the new version')
        assert.equal(textPattern(seatedToasts[0]), '# · #')
        assertUndo(seatedToasts[0])
        assert.ok(seat.init.arguments[1].elements.some((node) => node.name === 'onUndo'))
      }
    })

    it(`${surface.name}: the retained toast calls the current version Undo handler`, () => {
      const onUndo = declarations.get('onUndo')
      assert.ok(onUndo.start < completion.start, 'onUndo must be initialized before the completion hook dependencies')
      if (seat) assert.ok(onUndo.start < seat.start)
      assert.equal(onUndo.init.arguments[1].elements.length, 0, 'the retained callback must stay stable')
      const callback = onUndo.init.arguments[0]
      const undoActionRef = { current: () => 'old render' }
      const retained = new Function('undoActionRef', `return (${surface.source.slice(callback.start, callback.end)})`)(undoActionRef)
      assert.equal(retained(), 'old render')
      undoActionRef.current = () => 'current render'
      assert.equal(retained(), 'current render')
      let wired = false
      csuWalk(tree, (node) => {
        if (node.type === 'AssignmentExpression'
            && surface.source.slice(node.left.start, node.left.end) === 'undoActionRef.current'
            && node.right.type === 'Identifier' && node.right.name === surface.undoHandler) wired = true
      })
      assert.ok(wired, 'the current drawing Undo handler must feed the retained action')
      const handler = declarations.get(surface.undoHandler)
      assert.ok(surface.source.slice(handler.start, handler.end).includes('await undoDrawingVersion('))
    })

    it(`${surface.name}: delete, reset, authored removal and other toasts never receive version Undo`, () => {
      const attach = declarations.get(surface.name === 'App' ? 'onAttachAgentJob' : 'attachJob')
      const checkpointToasts = toastObjects(tree).filter((object) => property(object, 'key')?.value === 'agent-checkpoint')
      assert.equal(checkpointToasts.length, 1, 'one agent checkpoint notice may offer version Undo')
      const checkpoint = checkpointToasts[0]
      assert.ok(checkpoint.start > attach.start && checkpoint.end < attach.end, 'only the agent attach handler may raise the checkpoint')
      assert.equal(textPattern(checkpoint), 'Checkpoint saved')
      assert.equal(property(property(checkpoint, 'action'), 'label')?.value, 'Undo')
      const versionObjects = new Set([...versionToasts, ...seatedToasts, ...checkpointToasts])
      for (const object of toastObjects(tree)) {
        if (versionObjects.has(object)) continue
        const action = property(object, 'action')
        assert.notEqual(property(action, 'undo')?.value, true, `no Undo on ${textPattern(object) || 'non-version notice'}`)
        assert.notEqual(property(action, 'label')?.value, 'Undo')
        if (/project.*(?:deleted|reset)|tool.*removed|removed.*tool/i.test(textPattern(object))) {
          assert.equal(action, undefined, 'terminal completion toasts have no action')
        }
      }
      if (surface.name === 'ToolCast') {
        const deleted = toastObjects(declarations.get('forgetDeletedProject').init.arguments[0])
        assert.equal(deleted.length, 1)
        assert.equal(property(deleted[0], 'action'), undefined)
      }
    })
  }

  it('project reset and delete remain terminal in the shared lifecycle panel', () => {
    const source = readFileSync(new URL('./projects/ProjectLifecyclePanel.jsx', import.meta.url), 'utf8')
    const tree = parseJs(source, { sourceType: 'module', plugins: ['jsx'] })
    const dangerZones = []
    csuWalk(tree, (node) => {
      if (node.type === 'JSXOpeningElement' && node.name.type === 'JSXIdentifier' && node.name.name === 'DangerZone') dangerZones.push(node)
    })
    assert.equal(dangerZones.length, 1)
    const attributes = dangerZones[0].attributes
    assert.ok(attributes.some((node) => node.name?.name === 'onReset'))
    assert.ok(attributes.some((node) => node.name?.name === 'onDelete'))
    assert.ok(attributes.every((node) => node.type === 'JSXAttribute' && !['resetUndo', 'deleteUndo'].includes(node.name.name)))
    assert.equal(toastObjects(tree).length, 0, 'reset has a receipt, without an Undo toast')
  })
})

describe('project board live pane wiring', () => {
  const tree = parseJs(appSource, { sourceType: 'module', plugins: ['jsx'] })
  function elements(name) {
    const found = []
    csuWalk(tree, (node) => {
      if (node.type === 'JSXElement' && node.openingElement.name.type === 'JSXIdentifier'
          && node.openingElement.name.name === name) found.push(node)
    })
    return found
  }
  function expression(element, name) {
    const attr = element.openingElement.attributes.find((item) => item.name?.name === name)
    assert.ok(attr, `${name} must be wired`)
    return attr.value?.expression
  }
  function binding(element, name, expected) {
    const value = expression(element, name)
    assert.equal(appSource.slice(value.start, value.end), expected)
  }
  const paneSource = readFileSync(
    new URL('./workspace/ProjectWorkspacePanels.jsx', import.meta.url), 'utf8',
  )
  const paneTree = parseJs(paneSource, { sourceType: 'module', plugins: ['jsx'] })
  const pureNames = ['BOARD_PANE_REASONS', 'boardPaneReason', 'deriveBoardPaneSeats']
  const pureDeclarations = paneTree.program.body
    .filter((node) => node.type === 'ExportNamedDeclaration' && node.declaration)
    .map((node) => node.declaration)
    .filter((node) => pureNames.includes(
      node.type === 'VariableDeclaration' ? node.declarations[0].id.name : node.id?.name,
    ))
  function productionDerivation() {
    assert.equal(pureDeclarations.length, 3)
    return new Function(
      pureDeclarations.map((node) => paneSource.slice(node.start, node.end)).join('\n')
        + '\nreturn deriveBoardPaneSeats',
    )()
  }
  function source(node) {
    return appSource.slice(node.start, node.end)
  }
  function declaration(name) {
    const found = []
    csuWalk(tree, (node) => {
      if (node.type !== 'VariableDeclarator') return
      if (node.id.type === 'Identifier' && node.id.name === name) found.push(node)
      if (node.id.type === 'ObjectPattern'
          && node.id.properties.some((prop) => prop.value?.name === name)) found.push(node)
    })
    assert.equal(found.length, 1, `one live binding for ${name}`)
    return found[0]
  }
  function evaluate(node, scope) {
    return new Function(...Object.keys(scope), `return (${source(node)})`)(
      ...Object.values(scope),
    )
  }
  function bindingValue(name, scope) {
    const node = declaration(name)
    const value = evaluate(node.init, scope)
    if (node.id.type === 'Identifier') return value
    const prop = node.id.properties.find((item) => item.value?.name === name)
    assert.equal(csuKey(prop), name)
    return value[name]
  }
  function objectBindings(node) {
    assert.equal(node.type, 'ObjectExpression')
    return Object.fromEntries(node.properties.map((prop) => {
      const key = csuKey(prop)
      assert.ok(key)
      return [key, source(prop.value)]
    }))
  }
  function appSeats(patch = {}) {
    const imported = tree.program.body.find((node) =>
      node.type === 'ImportDeclaration'
      && node.source.value === './workspace/ProjectWorkspacePanels.jsx')
    assert.ok(imported?.specifiers.some((item) =>
      item.imported?.name === 'deriveBoardPaneSeats'
      && item.local.name === 'deriveBoardPaneSeats'))

    const call = declaration('paneSeats').init
    assert.equal(call.type, 'CallExpression')
    assert.equal(call.callee.name, 'deriveBoardPaneSeats')
    assert.equal(call.arguments.length, 1)
    const identities = [
      'boardHostsProject', 'projectPane', 'canConverse', 'agentMode', 'authorOpen',
      'conversationSource', 'conversationDestination', 'annotationSource',
      'annotationDestination', 'authorSource', 'authorDestination', 'authorFallback',
    ]
    assert.deepEqual(objectBindings(call.arguments[0]), {
      mock: 'mock', signedIn: 'signedIn', sessionStatus: 'session.status',
      projectId: 'openProjectId', drawingId: 'drawingState?.drawing_id',
      sessionId: 'agentSessionId',
      ...Object.fromEntries(identities.map((name) => [name, name])),
    })
    const scope = {
      mock: false, signedIn: true, session: { status: 'active' },
      openProjectId: 'p1', drawingState: { drawing_id: 'd1' },
      agentSessionId: 'session-a', boardHostsProject: true,
      projectPane: 'conversation', canConverse: true, agentMode: 'primary',
      authorOpen: false, conversationSource: 'conversation-source',
      conversationDestination: 'conversation-board',
      annotationSource: 'annotation-source', annotationDestination: 'annotation-board',
      authorSource: 'author-source', authorDestination: 'author-board',
      authorFallback: 'author-fallback',
      ...patch,
      deriveBoardPaneSeats: productionDerivation(),
    }
    const result = evaluate(call, scope)
    scope.paneSeats = result
    for (const name of [
      'boardPaneContext', 'boardConversation', 'boardAnnotations', 'boardAuthor',
      'conversationEligible', 'authorEligible', 'annotationEnabled',
    ]) {
      assert.deepEqual(bindingValue(name, scope), result[name], `${name} uses the derivation`)
    }
    return result
  }
  function ownerExpression(name) {
    const panel = elements(name)[0]
    assert.ok(panel)
    const found = []
    csuWalk(tree, (node) => {
      if (node.type === 'JSXExpressionContainer'
          && node.start < panel.start && node.end > panel.end) found.push(node)
    })
    found.sort((a, b) => (a.end - a.start) - (b.end - b.start))
    assert.ok(found[0])
    return found[0].expression
  }
  function guard(name, expected) {
    const node = ownerExpression(name)
    assert.equal(node.type, 'LogicalExpression')
    assert.equal(node.operator, '&&')
    assert.equal(source(node.left), expected)
  }
  function slot(name) {
    assert.equal(elements('ProjectWorkspacePanels').length, 1)
    const slots = expression(elements('ProjectWorkspacePanels')[0], 'slots')
    const prop = slots.properties.find((item) => csuKey(item) === name)
    assert.ok(prop, `App supplies ${name}`)
    return appSource.slice(prop.value.start, prop.value.end)
  }
  it('A2-01 wires Conversation to one shared session and persistent seat', () => {
    assert.ok(slot('conversation').includes('setConversationDestination'))
    assert.ok(slot('conversation').includes('ConversationOpening'))
    assert.ok(slot('conversation').includes('EntitlementNotice required="converse"'))
    const panels = elements('ConversePanel')
    assert.equal(panels.length, 1)
    for (const [name, value] of Object.entries({ sessionId: 'agentSessionId', userTurns: 'agentTurns',
      onAttachJob: 'onAttachAgentJob', onJobLinked: 'refreshJobs', engineDirty: 'engineDirty',
      onBeforeWriteApproval: 'closeStartForChange' })) binding(panels[0], name, value)
    const seats = elements('PersistentSeat')
    assert.ok(seats.some((seat) => seat.children.includes(panels[0])))
    binding(seats.find((seat) => seat.children.includes(panels[0])),
      'destination', 'paneSeats.conversationTarget')
    guard('ConversePanel', 'paneSeats.conversationMounted')
    const derived = appSeats()
    assert.equal(derived.conversationTarget, 'conversation-board')
    assert.equal(derived.conversationMounted, true)
    assert.ok(appSource.includes('attach: attachAgentSession, onOpen: openAgentMode'))
    assert.ok(appSource.includes("const boardHostsProject = boardVisible && surfaceSlots.ground === 'board'"))
    assert.ok(appSource.includes('if (mock || session.status !== \'active\') clearAgentSession()'))
    assert.ok(!appSource.includes('!signedIn || session.status !== \'active\') clearAgentSession()'))
  })
  it('A2-02 wires Annotations to the single App subscription and decision actions', () => {
    assert.ok(slot('annotations').includes('AnnotationPaneState annotations={annotations}'))
    assert.ok(slot('annotations').includes("setProjectPane('conversation')"))
    const panels = elements('AnnotationDecisionCard')
    assert.equal(panels.length, 1)
    for (const [name, value] of Object.entries({ annotation: 'annotations.annotation', busy: 'annotations.busy',
      error: 'annotations.error', confirmation: 'annotations.confirmation', onPreview: 'annotations.preview',
      onAccept: 'annotations.accept', onReject: 'annotations.reject', onRetry: 'annotations.retry', onUndo: 'annotations.undo' })) {
      binding(panels[0], name, value)
    }
    let owners = 0
    csuWalk(tree, (node) => { if (node.type === 'CallExpression' && node.callee.name === 'useAnnotations') owners += 1 })
    assert.equal(owners, 1)
    const owner = declaration('annotations').init
    assert.equal(owner.callee.name, 'useAnnotations')
    assert.equal(source(owner.arguments[0]), 'agentSessionId')
    assert.deepEqual(objectBindings(owner.arguments[1]), { enabled: 'annotationEnabled' })
    guard('AnnotationDecisionCard',
      'annotationEnabled && annotations.annotation && paneSeats.annotationTarget')
    const portal = ownerExpression('AnnotationDecisionCard').right
    assert.equal(portal.type, 'CallExpression')
    assert.equal(portal.callee.name, 'createPortal')
    assert.equal(source(portal.arguments[1]), 'paneSeats.annotationTarget')
    const live = appSeats({ projectPane: 'annotations' })
    assert.equal(live.annotationEnabled, true)
    assert.equal(live.annotationTarget, 'annotation-board')
    for (const patch of [
      { mock: true }, { signedIn: false }, { session: { status: 'signed_out' } },
    ]) {
      assert.equal(appSeats(patch).annotationEnabled, false)
    }
  })
  it('A2-03 wires Authoring to the existing stage and suppresses the rail fallback', () => {
    assert.ok(slot('authoring').includes('setAuthorDestination'))
    const panels = elements('AuthorPanel')
    assert.equal(panels.length, 1)
    for (const [name, value] of Object.entries({ onAuthor: 'onAuthor', onPublish: 'onPublishAuthor',
      onUseAuthored: 'onUseAuthored', seed: 'authorSeed', seedSignal: 'authorSignal', seedAutoSubmit: 'tourOn',
      targetToolName: 'authorTargetTool', onCancelRevision: 'onCancelAuthorRevision', stageActivity: 'authorStage',
      onResumeAuthor: 'authorStage.resume', notLinked: 'claudeNotLinked', buildEntitled: 'canBuild' })) {
      binding(panels[0], name, value)
    }
    const seats = elements('PersistentSeat')
    assert.ok(seats.some((seat) => seat.children.includes(panels[0])))
    binding(seats.find((seat) => seat.children.includes(panels[0])),
      'destination', 'paneSeats.authorTarget')
    const derived = appSeats({ projectPane: 'authoring' })
    assert.equal(derived.authorTarget, 'author-board')
    assert.equal(derived.authorMounted, true)
    assert.equal(elements('NavRail').length, 1)
    const override = expression(elements('NavRail')[0], 'authorContent')
    assert.ok(appSource.slice(override.start, override.end).includes('setAuthorSource'))
    const rail = readFileSync(new URL('./site/NavRail.jsx', import.meta.url), 'utf8')
    assert.ok(rail.includes('authorContent !== undefined ? authorContent : <AuthorPanel'))
    guard('AuthorPanel', 'paneSeats.authorMounted')
    for (const patch of [
      { mock: true }, { signedIn: false }, { session: { status: 'signed_out' } },
    ]) {
      assert.equal(appSeats({ projectPane: 'authoring', ...patch }).authorMounted, false)
    }
    let owners = 0
    csuWalk(tree, (node) => { if (node.type === 'CallExpression' && node.callee.name === 'useAuthorStageController') owners += 1 })
    assert.equal(owners, 1)
  })

  it('A2-26 derives the live board flag and returns seats to their sources', () => {
    const board = appSeats()
    assert.equal(board.boardConversation, true)
    assert.equal(board.conversationTarget, 'conversation-board')
    const sourceSeat = appSeats({ boardHostsProject: false })
    assert.equal(sourceSeat.boardConversation, false)
    assert.equal(sourceSeat.conversationTarget, 'conversation-source')
    assert.equal(sourceSeat.conversationMounted, true)
    const author = appSeats({
      boardHostsProject: false, projectPane: 'authoring',
      authorOpen: true, authorSource: null,
    })
    assert.equal(author.authorTarget, 'author-fallback')
    assert.equal(author.authorMounted, true)
  })

  it('A2-34 the rail author seat mounts for a signed-out or demo session', () => {
    for (const patch of [
      { authorOpen: true, signedIn: false },
      { authorOpen: true, mock: true },
      { authorOpen: true, session: { status: 'signed_out' } },
    ]) {
      const author = appSeats({ boardHostsProject: false, projectPane: null, ...patch })
      assert.equal(author.authorMounted, true)
      assert.equal(author.authorTarget, 'author-source')
    }
  })

  it('A2-35 a collapsed rail unmounts a signed-out author form and keeps a live one seated', () => {
    for (const patch of [
      { signedIn: false }, { mock: true }, { session: { status: 'signed_out' } },
    ]) {
      const author = appSeats({
        boardHostsProject: false, projectPane: null, authorOpen: true, authorSource: null, ...patch,
      })
      assert.equal(author.authorMounted, false)
    }
    const author = appSeats({
      boardHostsProject: false, projectPane: null, authorOpen: true, authorSource: null,
    })
    assert.equal(author.authorMounted, true)
    assert.equal(author.authorTarget, 'author-fallback')
  })

  function slotRefs(name) {
    assert.equal(elements('ProjectWorkspacePanels').length, 1)
    const slots = expression(elements('ProjectWorkspacePanels')[0], 'slots')
    const prop = slots.properties.find((item) => csuKey(item) === name)
    assert.ok(prop, `App supplies ${name}`)
    const refs = []
    csuWalk(prop.value, (node) => {
      if (node.type === 'JSXAttribute' && node.name?.name === 'ref') refs.push(source(node.value.expression))
    })
    return refs
  }
  it('A2-31 binds each board pane destination setter directly as the seat ref', () => {
    assert.deepEqual(slotRefs('conversation'), ['setConversationDestination'])
    assert.deepEqual(slotRefs('annotations'), ['setAnnotationDestination'])
    assert.deepEqual(slotRefs('authoring'), ['setAuthorDestination'])
  })
  it('A2-32 opens the author and the rail only while Authoring is the eligible board pane', () => {
    const effects = []
    csuWalk(tree, (node) => {
      if (node.type === 'CallExpression' && node.callee.name === 'useEffect'
          && source(node.arguments[0]).includes('boardAuthor && authorEligible')) effects.push(node)
    })
    assert.equal(effects.length, 1)
    assert.equal(source(effects[0].arguments[1]), '[boardAuthor, authorEligible]')
    for (const [boardAuthor, authorEligible, opens] of [[true, true, true], [true, false, false], [false, true, false]]) {
      const calls = []
      evaluate(effects[0].arguments[0], {
        boardAuthor, authorEligible,
        setAuthorOpenState: (value) => calls.push(['author', value]),
        setNavExpanded: (value) => calls.push(['nav', value]),
      })()
      assert.deepEqual(calls, opens ? [['author', true], ['nav', true]] : [])
    }
  })

  it('A2-28 binds attachment activation to live entitlement', () => {
    const opening = declaration('conversationOpening').init
    assert.equal(opening.callee.name, 'useBoardConversation')
    assert.deepEqual(objectBindings(opening.arguments[0]), {
      active: 'boardConversation', eligible: 'conversationEligible',
      sessionId: 'agentSessionId', context: 'conversationContext',
      attach: 'attachAgentSession', onOpen: 'openAgentMode',
    })
    assert.equal(appSeats({ agentSessionId: null }).conversationEligible, true)
    const denied = appSeats({ agentSessionId: null, canConverse: false })
    assert.equal(denied.boardConversation, true)
    assert.equal(denied.conversationEligible, false)
    assert.equal(denied.conversationMounted, false)
  })
})
const identitySource = readFileSync(new URL('./drawing/drawingIdentity.js', import.meta.url), 'utf8')
const selectionStart = identitySource.indexOf('export function hasDrawingSelection(')
const selectionEnd = identitySource.indexOf('export function isScopeSwitch(', selectionStart)
const hasDrawingSelection = new Function('return ' + identitySource.slice(selectionStart, selectionEnd).replace('export ', ''))()
const CSU_UPLOAD_READS = ['mock', 'openProjectId', 'signedIn', 'standalonePolicyReady']
function csuWalk(node, visit, parent = null) {
  if (!node || typeof node.type !== 'string') return
  visit(node, parent)
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end' || key === 'extra') continue
    const value = node[key]
    if (Array.isArray(value)) value.forEach((child) => csuWalk(child, visit, node))
    else if (value && typeof value.type === 'string') csuWalk(value, visit, node)
  }
}
function csuKey(prop) {
  if (!prop || prop.type !== 'ObjectProperty' || prop.computed) return null
  return prop.key.type === 'Identifier' ? prop.key.name : prop.key.type === 'StringLiteral' ? prop.key.value : null
}
// Binds to the real profileTabs useMemo by AST, never by text search, so a lookalike memo or object elsewhere
// cannot satisfy it, and only identifier reads count (a string or a member property named mock does not).
function csuUploadProblems(source) {
  const memos = []
  csuWalk(parseJs(source, { sourceType: 'module', plugins: ['jsx'] }), (node) => {
    if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier' && node.id.name === 'profileTabs'
      && node.init?.type === 'CallExpression' && node.init.callee.type === 'Identifier' && node.init.callee.name === 'useMemo') memos.push(node.init)
  })
  if (memos.length !== 1) return [`expected one profileTabs useMemo, found ${memos.length}`]
  const [callback, depsNode] = memos[0].arguments
  if (!depsNode || depsNode.type !== 'ArrayExpression') return ['the profileTabs memo has no dependency array']
  const deps = new Set(depsNode.elements.filter((el) => el?.type === 'Identifier').map((el) => el.name))
  const uploads = []
  csuWalk(callback, (node) => {
    if (node.type !== 'ObjectExpression') return
    const files = node.properties.find((prop) => csuKey(prop) === 'files')
    if (files?.value?.type !== 'ObjectExpression') return
    const upload = files.value.properties.find((prop) => csuKey(prop) === 'onUpload')
    if (upload) uploads.push(upload.value)
  })
  if (uploads.length !== 1) return [`expected one files.onUpload inside the profileTabs memo, found ${uploads.length}`]
  const reads = new Set()
  csuWalk(uploads[0], (node, parent) => {
    if (node.type !== 'Identifier') return
    if (parent && (parent.type === 'MemberExpression' || parent.type === 'OptionalMemberExpression') && parent.property === node && !parent.computed) return
    if (parent && parent.type === 'ObjectProperty' && parent.key === node && !parent.computed) return
    reads.add(node.name)
  })
  const problems = []
  for (const name of CSU_UPLOAD_READS) {
    if (!reads.has(name)) problems.push(`${name} is not read by the Upload drawing action`)
    if (!deps.has(name)) problems.push(`${name} must be a dependency of the ribbon memo`)
  }
  return problems
}

const formBToolCastSource = readFileSync(new URL('./site/ToolCast.jsx', import.meta.url), 'utf8')
const formBContext = { tenantId: 't1', drawingId: 'd1', drawingVersion: 3 }
const formBTool = {
  name: 'f', capabilities: [],
  params: { type: 'object', properties: { nullable_size: { type: ['number', 'null'], default: 3 } } },
}
function formBAst(source) {
  return parseJs(source, { sourceType: 'module', plugins: ['jsx'] })
}
function formBHookNode(source, name) {
  const found = []
  csuWalk(formBAst(source), (node) => {
    if (node.type === 'VariableDeclarator' && node.id.name === name
      && node.init?.callee?.name === 'useCallback') found.push(node.init.arguments[0])
  })
  assert.equal(found.length, 1, `one live ${name} callback`)
  return found[0]
}
function formBEvaluate(source, node, env) {
  return new Function(...Object.keys(env), `return (${source.slice(node.start, node.end)})`)(...Object.values(env))
}
function formBHook(source, name, env) {
  return formBEvaluate(source, formBHookNode(source, name), env)
}
function formBProp(source, component, prop, env) {
  const found = []
  csuWalk(formBAst(source), (node) => {
    if (node.type !== 'JSXOpeningElement' || node.name.name !== component) return
    const attr = node.attributes.find((item) => item.name?.name === prop)
    if (attr?.value?.expression?.type === 'ArrowFunctionExpression') found.push(attr.value.expression)
  })
  assert.equal(found.length, 1, `one ${component}.${prop}`)
  return formBEvaluate(source, found[0], env)
}
function formBHarness(shell) {
  const source = shell === 'App' ? appSource : formBToolCastSource
  const preparationCalls = []
  const requestCalls = []
  const decisions = []
  const stagedIntents = []
  const runIntentStateRef = { current: createRunIntentState('form-b') }
  const tools = [formBTool, { ...formBTool, name: 'solar-combiners' }]
  const env = {
    tools, mock: false, session: { status: 'active' }, canOperate: true,
    running: false, previewing: false, busy: false, jobRunning: false,
    writeLocked: false, canRunWrite: true, engineDirty: false,
    catalogRunContext: formBContext, catalogRunContextRef: { current: formBContext },
    runIntentStateRef, runIntentSessionRef: { current: 'form-b' }, runIntentSeqRef: { current: 0 },
    confirmEmitRef: { current: null }, tourDispatchRef: { current: false },
    checkout: { lockedByOther: null }, drawing: { mutationsBlocked: false }, previewLocked: false,
    isWriteTool: (tool) => (tool.capabilities || []).includes('drawing.write'),
    selectedHandle: null, ENV_SOLAR_SETTINGS_FORM: true,
    catalogRunOverlays: () => ({}), admittedOverlays: (_schema, overlays) => overlays,
    prepareCatalogRunParams: (...args) => {
      preparationCalls.push(args)
      return prepareCatalogRunParams(...args)
    },
    createCatalogToolSnapshot, dismissRunIntent,
    stageRunIntent: (...args) => {
      const staged = stageRunIntent(...args)
      stagedIntents.push(staged.intent)
      return staged
    },
    mintCorrelationId: () => String(stagedIntents.length + 1),
    track: () => {}, setRunErr: (message) => assert.fail(message),
    setError: (message) => assert.fail(message),
    setSelectedCatalogTool: () => {}, setLeftView: () => {},
  }
  if (shell === 'App') env.prepareRunParams = formBHook(source, 'prepareRunParams', env)
  const arm = formBHook(source, shell === 'App' ? 'armDecision' : 'armCatalogDecision', env)
  let lastArmed
  const commit = (decision) => {
    decisions.push(decision)
    lastArmed = arm(decision)
    return lastArmed
  }
  env.commitCatalogDecision = commit
  env.catalog = { actions: { commitDecision: commit } }
  const request = formBHook(source, shell === 'App' ? 'onRequestCatalogRun' : 'requestCatalogRun', env)
  const requestSpy = (...args) => {
    requestCalls.push(args)
    return request(...args)
  }
  env.onRequestCatalogRun = requestSpy
  env.requestCatalogRun = requestSpy
  return { source, env, tools, arm, request: requestSpy, preparationCalls, requestCalls,
    decisions, stagedIntents, get lastArmed() { return lastArmed } }
}
function formBAssertComplete(h, optionIndex) {
  assert.deepEqual(h.requestCalls.at(-1)[optionIndex], { complete: true })
  assert.equal(h.decisions.at(-1).paramsComplete, true)
  assert.deepEqual(h.preparationCalls.at(-1)[4], { complete: true })
  assert.deepEqual(h.lastArmed.params, {})
  assert.deepEqual(h.lastArmed.runIntent.params, {})
  assert.equal('paramsComplete' in h.lastArmed.runIntent, false)
  assert.equal('paramsComplete' in h.lastArmed.runIntent.params, false)
}

describe('complete submission provenance', () => {
  it('FORMB10 App form provenance reaches preparation', () => {
    for (const component of ['NavRail', 'SolarSettingsForm', 'SolarStepEditor', 'SolarToolForm']) {
      const h = formBHarness('App')
      Object.assign(h.env, {
        solarFormTool: formBTool, RIBBON_RATIONALE: 'Ribbon',
        settingsRunRef: { current: null }, setSettingsRunResult: () => {},
      })
      const prop = component === 'NavRail' ? 'onRequestRun' : 'onSubmit'
      const submit = formBProp(appSource, component, prop, h.env)
      if (component === 'SolarSettingsForm') submit({})
      else submit(formBTool, {})
      formBAssertComplete(h, 4)
    }
    for (const name of ['onSubmitSolarFlowStep', 'onRunCombinerPlacement']) {
      const h = formBHarness('App')
      Object.assign(h.env, {
        RIBBON_RATIONALE: 'Ribbon', MAX_FLOW_STEPS: 20,
        solarFlowRetainedRef: { current: new Map() }, solarFlowRunRef: { current: null },
        setSolarFlowPending: () => {},
      })
      const submit = formBHook(appSource, name, h.env)
      if (name === 'onRunCombinerPlacement') assert.equal(submit({}), true)
      else submit(formBTool, {})
      formBAssertComplete(h, 4)
    }
  })

  it('FORMB11 ToolCast form provenance reaches preparation', () => {
    const h = formBHarness('ToolCast')
    formBProp(h.source, 'CapabilityCatalog', 'onRequestRun', h.env)(formBTool, {})
    formBAssertComplete(h, 2)
  })

  it('FORMB12 partial entry points remain partial', async () => {
    const scheduledScrolls = []
    for (const shell of ['App', 'ToolCast']) {
      const h = formBHarness(shell)
      const assertPartial = (armed) => {
        assert.deepEqual(armed.params, { nullable_size: 3 })
        assert.deepEqual(h.preparationCalls.at(-1)[4], { complete: false })
      }
      // Controller commits for router, slash, and alternative selections share the live arm.
      for (const decision of [
        { lane: 'run', tool: formBTool.name, params: {}, source: 'prompt' },
        slashDecision('/f', h.tools).decision,
        alternativeDecision({ alternatives: [] }, 'f'),
      ]) {
        assert.equal(decision.paramsComplete, undefined)
        assertPartial(h.arm(decision))
      }
      // Nonempty submitted values alone cannot imply completeness either.
      assertPartial(h.arm({ lane: 'run', tool: 'f', params: {}, source: 'catalog' }))
      if (shell === 'App') {
        // Execute the actual ribbon and tour call expressions, whose params are partial.
        for (const origin of ['ribbon', 'tour']) {
          const calls = []
          csuWalk(formBAst(appSource), (node) => {
            if (node.type !== 'CallExpression' || node.callee.name !== 'onRequestCatalogRun') return
            if (node.arguments[3]?.value !== origin || node.arguments.length !== 4) return
            if (origin === 'ribbon' && node.arguments[1]?.type !== 'NullLiteral') return
            calls.push(node)
          })
          assert.equal(calls.length, 1, `one partial ${origin} entry`)
          formBEvaluate(appSource, calls[0], {
            ...h.env, tool: formBTool, toolObj: formBTool,
            r: { params: {} }, RIBBON_RATIONALE: 'Ribbon',
          })
          assertPartial(h.lastArmed)
          assert.equal(h.decisions.at(-1).paramsComplete, false)
        }
      } else {
        h.request(formBTool, null)
        assertPartial(h.lastArmed)
        assert.equal(h.decisions.at(-1).paramsComplete, false)
      }
      // Execute author-after-publish using a resolved catalog row, keeping admission gates live.
      Object.assign(h.env, {
        sessionReady: true, loadCatalogTools: async () => h.tools,
        resolvePublishedCatalogTool: (_published, rows) => rows.find((row) => row.name === 'f'),
        setLastAuthoredTool: () => {}, showToast: (message) => assert.fail(JSON.stringify(message)),
        // App's onUseAuthored schedules a scroll through setTimeout and document; node:test has no document,
        // so the stub records the call instead of leaking a timer past the row.
        setTimeout: (callback) => { scheduledScrolls.push(callback); return 0 },
      })
      h.env.catalog.actions.loadTools = async () => h.tools
      await formBHook(h.source, shell === 'App' ? 'onUseAuthored' : 'useAuthoredTool', h.env)(formBTool)
      assert.equal(h.decisions.at(-1).paramsComplete, undefined)
      assertPartial(h.lastArmed)
    }
  })

  it('FORMB13 retry and resume preserve omission', async () => {
    for (const path of ['app-retry', 'app-resume', 'toolcast-retry']) {
      const h = formBHarness(path === 'toolcast-retry' ? 'ToolCast' : 'App')
      const previous = h.arm({ lane: 'run', tool: 'f', params: {}, paramsComplete: true })
      const execute = () => assert.fail('recovery must stage a fresh intent for confirmation')
      Object.assign(h.env, {
        lastRunRef: { current: { tool: formBTool, params: previous.params } },
        lastConfirmedRunRef: { current: { tool: formBTool, params: previous.params } },
        preparePendingRun: async (load) => ({
          tool: (await load()).find((tool) => tool.name === 'f'),
          pending: { params: previous.params },
        }),
        getTools: async () => h.tools,
        onRun: execute, runCatalogTool: execute, runTool: execute, runToolAsync: execute,
      })
      const name = path === 'app-retry' ? 'onRetry'
        : path === 'app-resume' ? 'onResumePendingRun' : 'retryCatalogRun'
      await formBHook(h.source, name, h.env)()
      formBAssertComplete(h, path === 'toolcast-retry' ? 2 : 4)
      const next = h.lastArmed.runIntent
      assert.notEqual(next.intentId, previous.runIntent.intentId)
      assert.equal(h.env.runIntentStateRef.current.pending, next)
      assert.deepEqual(h.env.runIntentStateRef.current.consumedIds, [])
      // The former confirm card cannot authorize the newly staged recovery request.
      assert.equal(confirmRunIntent(h.env.runIntentStateRef.current, previous.runIntent).code, 'changed_intent')
      const confirmed = confirmRunIntent(h.env.runIntentStateRef.current, next)
      assert.equal(confirmed.ok, true)
      assert.deepEqual(confirmed.execution.params, {})
    }
  })
})

describe('W20-07b combiner workspace wiring', () => {
  const appNoComments = decomment(appSource)
  it('PRD6 App passes a catalog digest lookup built from its catalog rows to the workspace', () => {
    const mountStart = appNoComments.indexOf('<SolarWorkspaceTools')
    assert.ok(mountStart >= 0)
    const mount = appNoComments.slice(mountStart, appNoComments.indexOf('/>', mountStart))
    assert.ok(mount.includes('catalogDigestOf={catalogDigestOf}'))
    const start = appNoComments.indexOf('const catalogDigestOf = useCallback(')
    const end = appNoComments.indexOf('const onRunCombinerPlacement', start)
    assert.ok(start >= 0 && end > start)
    const body = appNoComments.slice(start, end).trim()
    assert.ok(body.includes('tools.find((tool) => tool.name === name)'))
    assert.ok(body.includes('createCatalogToolSnapshot(row).catalogDigest'))
    assert.ok(body.endsWith('}, [tools])'))
  })

  it('W20-07b mounts the placement callback on the existing workspace container', () => {
    const start = appNoComments.indexOf('<SolarWorkspaceTools')
    assert.ok(start >= 0)
    const mount = appNoComments.slice(start, appNoComments.indexOf('/>', start))
    assert.ok(mount.includes('onRunPlacement={onRunCombinerPlacement}'))
    assert.ok(mount.includes('onDrawingVersionChanged={seatCompletedVersion}'))
  })

  it('W20-07b resolves the catalog row and stages through the confirm strip without the rail', () => {
    const start = appNoComments.indexOf('const onRunCombinerPlacement = useCallback')
    const end = appNoComments.indexOf('const onSubmitSolarFlowStep', start)
    assert.ok(start >= 0 && end > start)
    const body = appNoComments.slice(start, end)
    assert.ok(body.includes("tools.find((tool) => tool.name === 'solar-combiners')"))
    const absent = body.indexOf('if (!row)')
    const message = body.indexOf("setRunErr('Combiner placement is not in this catalog.')")
    const refusal = body.indexOf('return false', absent)
    const stage = body.indexOf("return Boolean(onRequestCatalogRun(row, params, RIBBON_RATIONALE, 'ribbon', { complete: true })?.runIntent?.intentId)")
    assert.ok(absent >= 0 && message > absent && refusal > message && stage > refusal)
    assert.ok(body.includes('[onRequestCatalogRun, tools]'))
    assert.equal(body.includes('onSubmitSolarFlowStep('), false)
  })

  it('W20-07b guards successful and failed head reads and seats quietly when requested', () => {
    const start = appNoComments.indexOf('const seatCompletedVersion = useCallback')
    const end = appNoComments.indexOf('completedVersionRef.current = seatCompletedVersion', start)
    assert.ok(start >= 0 && end > start)
    const body = appNoComments.slice(start, end)
    assert.ok(body.includes('async (newVersion, envelope, options)'))
    const read = body.indexOf("await getDrawingIntake(mock, newVersion.drawing_id, 'head')")
    assert.ok(body.includes('const scopeCurrent = isScopeCurrent'))
    assert.match(body, /const current = \(\) => scopeCurrent\(\)\s+&& \(typeof options\?\.isCurrent !== 'function' \|\| options\.isCurrent\(\)\)/)
    assert.ok(body.includes('[intake, isScopeCurrent, markRefreshFailure, mock, onUndo, recordCommittedUnreadableHead, seatVersion, showToast]'))
    const guard = 'if (!current()) return false'
    const entryGuard = body.indexOf(guard)
    assert.ok(entryGuard > body.indexOf('const current =') && entryGuard < body.indexOf('let version ='))
    const mockCatch = body.indexOf('catch')
    const mockGuard = body.indexOf(guard, mockCatch)
    assert.ok(mockCatch >= 0 && mockGuard > mockCatch && mockGuard < body.indexOf('showToast(', mockCatch))
    const unreadable = body.indexOf('if (envelope?.result?.new_version_readable === false)')
    const unreadableGuard = body.indexOf(guard, unreadable)
    assert.ok(unreadable >= 0 && unreadableGuard > unreadable && unreadableGuard < body.indexOf('recordCommittedUnreadableHead(', unreadable))
    const successGuard = body.indexOf(guard, read)
    const seat = body.indexOf('seatVersion(', read)
    const accepted = body.indexOf('return true', seat)
    const caught = body.indexOf('catch', seat)
    const failureGuard = body.indexOf(guard, caught)
    const toast = body.indexOf('showToast(', caught)
    const failed = body.indexOf('markRefreshFailure(', caught)
    const rejected = body.indexOf('return false', failed)
    assert.ok(read >= 0 && successGuard > read && seat > successGuard && accepted > seat && caught > accepted)
    assert.ok(failureGuard > caught && toast > failureGuard && failed > toast && rejected > failed)
    assert.ok(body.slice(seat, accepted).includes('options?.announce === false ? null :'))
    assert.ok(body.slice(caught, failed).includes('if (options?.announce !== false) showToast'))
    assert.equal(body.split('return true').length, 2)
  })
})

function maskSeatCss(cssText) {
  const masked = cssText.split('')
  for (let i = 0; i < cssText.length; i += 1) {
    if (cssText.startsWith('/*', i)) {
      const end = cssText.indexOf('*/', i + 2)
      const stop = end < 0 ? cssText.length : end + 2
      for (; i < stop; i += 1) masked[i] = ' '
      i -= 1
    } else if (cssText[i] === '"' || cssText[i] === "'") {
      const quote = cssText[i]
      masked[i] = ' '
      for (i += 1; i < cssText.length; i += 1) {
        masked[i] = ' '
        if (cssText[i] === '\\') {
          i += 1
          if (i < cssText.length) masked[i] = ' '
        } else if (cssText[i] === quote) break
      }
    }
  }
  return masked.join('')
}

function seatCssStructure(cssText) {
  const css = maskSeatCss(cssText)
  const headers = []
  const rules = []
  const occurrences = []
  const failures = []
  let headerStart = 0
  for (let i = 0; i < css.length; i += 1) {
    if (css.startsWith('.solar-flow-seat', i)) occurrences.push({ index: i, headers: headers.slice() })
    if (css[i] === '{') {
      const header = css.slice(headerStart, i).trim()
      rules.push({ header, headers: headers.slice() })
      headers.push(header)
      headerStart = i + 1
    } else if (css[i] === '}') {
      if (headers.length === 0) failures.push('CSS closing brace has no opening brace')
      else headers.pop()
      headerStart = i + 1
    } else if (css[i] === ';') headerStart = i + 1
  }
  if (headers.length) failures.push('CSS opening braces remain unclosed')
  return { rules, occurrences, failures }
}

function seatRulePlacementFailures(cssText) {
  const { rules, occurrences, failures } = seatCssStructure(cssText)
  if (occurrences.length < 5) failures.push('seat rules must be present')
  for (const { index, headers } of occurrences) {
    if (!headers.some((header) => header.startsWith('@media '))) failures.push('seat occurrence outside media at ' + index)
  }
  for (const selector of [
    '.studio-shell .app[data-start-open="true"] .solar-flow-seat',
    '.studio-shell .app[data-surface="solar"] .workspace-card[data-cockpit-picking="1"] :is(.solar-flow-seat, .solar-flow-seat *)',
    '.studio-shell .app[data-surface="solar"] .workspace-card[data-cockpit-picking="1"] .solar-flow-seat',
  ]) {
    const matches = rules.filter(({ header }) => header === maskSeatCss(selector))
    if (matches.length !== 1) failures.push('expected one rule for ' + selector)
    for (const { headers } of matches) {
      if (!headers.some((header) => header.startsWith('@media ') && header.includes('min-width: 981px') && !header.includes('max-width'))) {
        failures.push('rule must be inside unrestricted desktop media: ' + selector)
      }
    }
  }
  return failures
}

describe('report object origin wiring', () => {
  it('mounts the origin bridge inside the object provider and passes its key to jobs', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const provider = live.search(/React\.createElement\(\s*DrawingObjectsProvider,/)
    const bridge = provider + live.slice(provider).search(/React\.createElement\(\s*DrawingOriginBridge,/)
    const shell = provider + live.slice(provider).search(/React\.createElement\(\s*SurfaceFrame,/)
    assert.ok(provider >= 0 && bridge > provider && shell > bridge)
    assert.match(live.slice(bridge, shell), /onChange: setResultDrawingKey/)
    assert.match(live, /drawingKey: resultDrawingKey/)
    assert.match(live, /\[resultDrawingKey, setResultDrawingKey\] = useState\(null\)/)
  })
  it('passes named navigation to the result panel and suppresses mismatched handles only', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const panel = live.search(/React\.createElement\(\s*ResultPanel,/)
    assert.ok(panel >= 0)
    assert.match(live.slice(panel, panel + 250), /navigation: viewNavigation/)
    assert.match(live, /typeof result\?\.origin\?\.drawingKey === "string" && result\.origin\.drawingKey\.length > 0 && resultDrawingKey && result\.origin\.drawingKey !== resultDrawingKey/)
    const overlay = live.slice(live.indexOf('const overlay ='), live.indexOf('const applied ='))
    assert.match(overlay, /!overlayStale/)
    assert.match(overlay, /resultDrawingMismatch && result\.overlay/)
    assert.match(overlay, /\.\.\.result\.overlay,\s*highlight_handles: \[\]/)
  })
})
describe('report overlay drawing identity', () => {
  const start = appSource.indexOf('const resultDrawingMismatch =')
  const end = appSource.indexOf('const applied =', start)
  assert.ok(start >= 0 && end > start, 'App overlay derivation exists')
  const derive = new Function('result', 'resultDrawingKey', 'overlayStale',
    appSource.slice(start, end) + '\nreturn overlay')

  it('keeps highlights after the index loads when origin is absent or has no key', () => {
    const overlay = { highlight_handles: ['AB'], markers: [{ pt: [1, 2] }] }
    for (const origin of [undefined, { drawingKey: null }, { drawingKey: '' }]) {
      const result = origin === undefined ? { overlay } : { overlay, origin }
      assert.equal(derive(result, 'engine:loaded-v1.dxf', false), overlay)
    }
  })

  it('suppresses only highlights for a known different origin and still respects stale overlays', () => {
    const overlay = { highlight_handles: ['AB'], markers: [{ pt: [1, 2] }], polylines: [] }
    const result = { overlay, origin: { drawingKey: 'engine:first-v1.dxf' } }
    assert.deepEqual(derive(result, 'engine:second-v1.dxf', false), { ...overlay, highlight_handles: [] })
    assert.equal(derive(result, 'engine:first-v1.dxf', false), overlay)
    assert.equal(derive(result, null, false), overlay)
    assert.equal(derive(result, 'engine:first-v1.dxf', true), null)
  })
})
const viewerSource = readFileSync(new URL('./components/Viewer.jsx', import.meta.url), 'utf8')
const occluderSource = decomment(readFileSync(new URL('./site/drawingOccluders.js', import.meta.url), 'utf8'))

describe('Conductor form wiring', () => {
  it('SZ23 the live ribbon branch routes conductors and sizing to the step editor', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const start = live.indexOf('["conductors", "sizing", "strings"].includes(solarSettingsFormChoice(')
    assert.ok(start >= 0)
    const branch = live.slice(start, start + 1600)
    assert.match(branch, /React.createElement\(\s*SolarStepEditor/)
    assert.match(branch, /readIntake: SOLAR_SETTINGS_LOADERS.readIntake/)
    assert.match(branch, /drawingVersion: catalogRunContext\?\.drawingVersion/)
    assert.match(branch, /onSubmit: \(tool, params\) => onRequestCatalogRun\(tool, params, RIBBON_RATIONALE, "ribbon", \{ complete: true \}\)/)
  })

  it('CF15 the ribbon conductor choice mounts the step editor with live context and loaders', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const start = live.indexOf('["conductors", "sizing", "strings"].includes(solarSettingsFormChoice(')
    assert.ok(start >= 0)
    const host = live.slice(start, start + 1600)
    assert.match(host, /React.createElement\(\s*SolarStepEditor/)
    assert.match(host, /row: solarFormTool/)
    assert.match(host, /drawingId: catalogRunContext\?\.drawingId/)
    assert.match(host, /drawingVersion: catalogRunContext\?\.drawingVersion/)
    assert.match(host, /projectId: catalogRunContext\?\.projectId/)
    assert.match(host, /readIntake: SOLAR_SETTINGS_LOADERS.readIntake/)
    assert.match(host, /onSubmit: \(tool, params\) => onRequestCatalogRun\(tool, params, RIBBON_RATIONALE, "ribbon", \{ complete: true \}\)/)
    assert.match(host, /onClose: \(\) => setSolarFormTool\(null\)/)
  })

  it('CF8 the rail editor receives project scope and CF14 overlays receive the tool name', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const rail = live.slice(live.indexOf('row: solarFlowEditor'), live.indexOf('row: solarFlowEditor') + 700)
    assert.match(rail, /projectId: catalogRunContext\?\.projectId/)
    const start = live.indexOf('catalogRunOverlays({ enabled')
    assert.ok(start >= 0)
    assert.match(live.slice(start, start + 220), /toolName: tool\?\.name/)
  })

  it('H12 the ribbon string choice keeps the step editor and generic fallback', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const choice = '["conductors", "sizing", "strings"].includes(solarSettingsFormChoice('
    assert.equal(live.split(choice).length - 1, 1)
    const start = live.indexOf(choice)
    const mount = live.slice(start).search(/React\.createElement\(\s*SolarStepEditor\b/)
    assert.ok(mount > 0 && mount < 1600)
    const fallback = live.slice(start + mount).search(/React\.createElement\(\s*SolarToolForm,/)
    assert.ok(fallback > 0)
  })
})

describe('Solar settings form wiring', () => {
  it('SF2 wiring the typed form mounts at the Solar form host behind its fence', () => {
    const host = appSource.indexOf("{ENV_CAD_EDIT && drafting && surfaceSlots.toolbar.profile === 'solar' && solarFormTool && (")
    const fence = appSource.indexOf('ENV_SOLAR_SETTINGS_FORM && solarSettingsFormChoice(', host)
    const typed = appSource.indexOf('<SolarSettingsForm', fence)
    const generic = appSource.indexOf('<SolarToolForm', host)
    assert.ok(host >= 0 && fence > host && typed > fence && generic > typed)
  })

  it('SF2 wiring prepareRunParams takes its overlays from catalogRunOverlays', () => {
    const start = appSource.indexOf('const prepareRunParams = useCallback')
    const end = appSource.indexOf('}, [selectedHandle])', start)
    assert.ok(start >= 0 && end > start)
    assert.ok(appSource.slice(start, end).includes('catalogRunOverlays({'))
    assert.ok(!appSource.includes('? { target_handle: selectedHandle, ...(isWrite ? { handle: selectedHandle } : {}) }'))
  })

  it('SF2 wiring the catalog controller context follows solarSettingsScope', () => {
    assert.ok(appSource.includes('catalogController.setContext(solarSettingsScope('))
  })

  it('SF2 wiring a settings submit ties the run to its own intent and drawing', () => {
    const formStart = appSource.indexOf('<SolarSettingsForm')
    assert.notEqual(formStart, -1)
    const marker = 'onSubmit={(params) => {'
    const submitStart = appSource.indexOf(marker, formStart)
    assert.ok(submitStart > formStart)
    const bodyStart = submitStart + marker.length - 1
    let depth = 1
    let bodyEnd = bodyStart + 1
    for (; bodyEnd < appSource.length && depth > 0; bodyEnd++) {
      if (appSource[bodyEnd] === '{') depth++
      if (appSource[bodyEnd] === '}') depth--
    }
    assert.equal(depth, 0, 'submit body must close')
    const submit = new Function('onRequestCatalogRun', 'solarFormTool', 'RIBBON_RATIONALE',
      'catalogRunContext', 'settingsRunRef', 'setSettingsRunResult', 'params',
      appSource.slice(bodyStart + 1, bodyEnd - 1))
    const solarFormTool = { tool: 'settings sentinel' }
    const params = { params: 'sentinel' }
    const rationale = { rationale: 'sentinel' }
    for (const [armed, context, expected] of [
      [{ runIntent: { intentId: 'i-1' } }, { drawingId: 'd1', drawingVersion: 3 },
        { intentId: 'i-1', drawingId: 'd1', drawingVersion: 3 }],
      [undefined, { drawingId: 'd1', drawingVersion: 3 }, null],
      [{ runIntent: {} }, { drawingId: 'd1', drawingVersion: 3 }, null],
      [{ runIntent: { intentId: 'i-2' } }, { drawingId: 'd2', drawingVersion: 7 },
        { intentId: 'i-2', drawingId: 'd2', drawingVersion: 7 }],
    ]) {
      const settingsRunRef = { current: { stale: true } }
      const resultCalls = []
      const requestCalls = []
      submit((...args) => { requestCalls.push(args); return armed }, solarFormTool, rationale,
        context, settingsRunRef, (value) => resultCalls.push(value), params)
      assert.deepEqual(settingsRunRef.current, expected)
      assert.deepEqual(resultCalls, [null])
      assert.equal(requestCalls.length, 1)
      assert.equal(requestCalls[0].length, 5)
      assert.equal(requestCalls[0][0], solarFormTool)
      assert.equal(requestCalls[0][1], params)
      assert.equal(requestCalls[0][2], rationale)
      assert.equal(requestCalls[0][3], 'ribbon')
    }
  })

  it('SF2 wiring any change of the open Solar form clears the settings run association', () => {
    const commentStart = appSource.indexOf('// The settings run result belongs to one open form')
    assert.notEqual(commentStart, -1)
    const marker = 'useLayoutEffect(() => {'
    const effectStart = appSource.indexOf(marker, commentStart)
    assert.ok(effectStart > commentStart)
    const hookStart = appSource.indexOf('useLayoutEffect(', commentStart)
    const effectLineStart = appSource.lastIndexOf('\n', hookStart) + 1
    const effectLineEnd = appSource.indexOf('\n', hookStart)
    assert.match(appSource.slice(effectLineStart, effectLineEnd === -1 ? appSource.length : effectLineEnd),
      new RegExp('^[ \\t]*useLayoutEffect\\(\\(\\) => \\{\\r?$'))
    const bodyStart = effectStart + marker.length - 1
    let depth = 1
    let bodyEnd = bodyStart + 1
    for (; bodyEnd < appSource.length && depth > 0; bodyEnd++) {
      if (appSource[bodyEnd] === '{') depth++
      if (appSource[bodyEnd] === '}') depth--
    }
    assert.equal(depth, 0, 'effect body must close')
    assert.equal(appSource.slice(bodyEnd, bodyEnd + 2), ', ')
    const dependencyEnd = appSource.indexOf(')', bodyEnd)
    assert.ok(dependencyEnd > bodyEnd)
    assert.equal(appSource.slice(bodyEnd + 2, dependencyEnd), '[solarFormTool]')
    const clear = new Function('ENV_SOLAR_SETTINGS_FORM', 'settingsRunRef', 'setSettingsRunResult',
      appSource.slice(bodyStart + 1, bodyEnd - 1))
    for (const enabled of [true, false]) {
      const association = { intentId: 'i-1' }
      const settingsRunRef = { current: association }
      const resultCalls = []
      clear(enabled, settingsRunRef, (value) => resultCalls.push(value))
      assert.equal(settingsRunRef.current, enabled ? null : association)
      assert.deepEqual(settingsRunRef.current, enabled ? null : { intentId: 'i-1' })
      assert.deepEqual(resultCalls, enabled ? [null] : [])
    }
    const compiled = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    assert.match(compiled, new RegExp('useLayoutEffect\\([\\s\\S]{0,300}settingsRunRef\\.current = null'))
    const compiledEffectStart = compiled.match(new RegExp('useLayoutEffect\\([\\s\\S]{0,300}settingsRunRef\\.current = null')).index
    const compiledLineStart = compiled.lastIndexOf('\n', compiledEffectStart) + 1
    assert.match(compiled.slice(compiledLineStart, compiledEffectStart), new RegExp('^\\s*$'))
  })

  it('SF2 wiring a confirmed settings run records its result for the form', () => {
    const start = appSource.indexOf('const onConfirmCatalogRun = useCallback')
    const end = appSource.indexOf('}, [dismissRoute, mock, onRun])', start)
    assert.ok(start >= 0 && end > start)
    const body = appSource.slice(start, end)
    assert.ok(body.includes('settingsRunRef.current?.intentId === confirmed.execution.intentId'))
    assert.ok(body.includes('setSettingsRunResult('))
  })
})

describe('solar-ui-rail wiring', () => {
  it('passes the solar rail only under the engine flag on a drafting surface', () => {
    assert.match(appSource, /solarRail: ENV_CAD_EDIT && drafting \? \{\s*families: catalog\.families, openName: solarFormTool\?\.name \?\? null, onOpenForm: setSolarFormTool,?\s*\} : null/)
    const memo = appSource.slice(appSource.indexOf('const profileTabs = useMemo'), appSource.indexOf('const previousRibbonProfile'))
    const deps = memo.slice(memo.lastIndexOf('}, ['))
    for (const dependency of ['catalog.families', 'solarFormTool', 'drafting']) assert.ok(deps.includes(dependency))
    assert.ok(memo.includes('solar: { status: solarRoutesStatus, shown: showSolarStrings'))
  })

  it('mounts SolarToolForm before the drafting ribbon under the engine flag', () => {
    assert.ok(appSource.includes("{ENV_CAD_EDIT && drafting && surfaceSlots.toolbar.profile === 'solar' && solarFormTool && ("))
    const card = appSource.indexOf('className="workspace-card enter"')
    const form = appSource.indexOf('<SolarToolForm')
    const ribbon = appSource.indexOf('{studioShell && surfaceSlots.toolbar.ribbon && studioRibbonHost && createPortal(')
    assert.ok(card >= 0 && form > card && ribbon > form)
    assert.match(appSource.slice(form, ribbon), /key=\{solarFormTool\.name\}/)
    assert.ok(appSource.includes("activeRibbonTab === 'solar' ? ['solar-panels']"))
    assert.ok(appSource.includes('id="cockpit-solar-panels-slot"'))
  })

  it('commits the form through the catalog run path', () => {
    const form = appSource.slice(appSource.indexOf('<SolarToolForm'), appSource.indexOf('{/* W4c-V1: the drafting ribbon'))
    assert.ok(form.includes("onSubmit={(tool, params) => onRequestCatalogRun(tool, params, RIBBON_RATIONALE, 'ribbon', { complete: true })}"))
    assert.ok(form.includes('onClose={() => setSolarFormTool(null)}'))
    assert.ok(appSource.includes('const [solarFormTool, setSolarFormTool] = useState(null)'))
  })
})

describe('Solar step rail wiring', () => {
  const condition = "{ENV_SOLAR_FLOW_RAIL && ENV_CAD_EDIT && ENV_SOLAR_SETTINGS_FORM && drafting && surfaceSlots.toolbar.profile === 'solar' && ("

  it('FR wiring the rail mounts once, behind its fence, after the Solar form host and before the ribbon', () => {
    const host = appSource.indexOf("{ENV_CAD_EDIT && drafting && surfaceSlots.toolbar.profile === 'solar' && solarFormTool && (")
    const generic = appSource.indexOf('<SolarToolForm', host)
    const mount = appSource.indexOf(condition)
    const rail = appSource.indexOf('<SolarFlowRail', mount)
    const ribbon = appSource.indexOf('{/* W4c-V1: the drafting ribbon')
    assert.ok(host >= 0 && generic > host && mount > generic && rail > mount && ribbon > rail)
    assert.equal(appSource.split('<SolarFlowRail').length, 2)
    assert.equal(appSource.split(condition).length, 2)
    assert.ok(appSource.slice(rail, ribbon).includes('families={catalog.families}'))
  })

  it('FR wiring the rail flag is the first operand so a flag-off build folds the mount away', () => {
    const mount = appSource.indexOf(condition)
    assert.ok(mount >= 0)
    const operands = appSource.slice(mount + 1, appSource.indexOf(' && (', mount)).split(' && ')
    assert.equal(operands[0], 'ENV_SOLAR_FLOW_RAIL')
    for (const operand of ['ENV_CAD_EDIT', 'ENV_SOLAR_SETTINGS_FORM', 'drafting', "surfaceSlots.toolbar.profile === 'solar'"]) {
      assert.ok(operands.includes(operand), `${operand} must gate the rail`)
    }
  })

  it('FR wiring a step submit arms through the catalog run path and never runs a tool', () => {
    const start = appSource.indexOf('const onSubmitSolarFlowStep = useCallback')
    const end = appSource.indexOf('}, [onRequestCatalogRun])', start)
    assert.ok(start >= 0 && end > start)
    const body = appSource.slice(start, end)
    assert.ok(body.includes("onRequestCatalogRun(row, params, RIBBON_RATIONALE, 'ribbon', { complete: true })"))
    assert.ok(!/\brunTool(Async)?\(/.test(body))
  })

  it('FR wiring a confirmed step run records its outcome from its own settle, under the rail flag', () => {
    const start = appSource.indexOf('const onConfirmCatalogRun = useCallback')
    const end = appSource.indexOf('}, [dismissRoute, mock, onRun])', start)
    assert.ok(start >= 0 && end > start)
    const body = appSource.slice(start, end)
    const fence = body.indexOf('if (ENV_SOLAR_FLOW_RAIL) {')
    assert.ok(fence > body.indexOf('const runPromise = onRun('))
    assert.ok(body.indexOf('staged?.intentId === intentId', fence) > fence)
    assert.ok(body.indexOf('solarFlowRecordRun(previous, flowRun, envelope)', fence) > fence)
  })

  it('CORR1 wiring App passes the ribbon opener predicate and the families drawing id to the rail', () => {
    const opener = 'ENV_SOLAR_SETTINGS_FORM && !mock && catalogRunContext?.projectId === null ? canOpenSolarSettingsForm : undefined'
    assert.ok(appSource.includes(`solarTypedForm: ${opener},`))
    const rail = appSource.indexOf('<SolarFlowRail')
    assert.ok(rail >= 0)
    const props = appSource.slice(rail, appSource.indexOf('/>', rail))
    assert.ok(props.includes(`openSettingsForm={${opener}}`))
    assert.ok(props.includes('familiesDrawingId={solarFlowFamiliesDrawingId}'))
    const record = appSource.indexOf('setSolarFlowFamiliesDrawingId(scope.drawingId ?? null)')
    const scope = appSource.lastIndexOf('const scope = solarSettingsScope({ enabled: ENV_SOLAR_SETTINGS_FORM, mock, profile: surfaceSlots.toolbar.profile, context: catalogRunContext })', record)
    const handed = appSource.indexOf('catalogController.setContext(solarSettingsScope(')
    assert.ok(handed >= 0 && scope > handed && record > scope)
  })

  it('CORR1 wiring the dismiss path and the refused confirms clear solarFlowPending', () => {
    const start = appSource.indexOf('const clearSolarFlowStaged = useCallback(')
    const end = appSource.indexOf('}, [])', start)
    assert.ok(start >= 0 && end > start)
    const clear = new Function('solarFlowRunRef', 'setSolarFlowPending',
      `return ${appSource.slice(start + 'const clearSolarFlowStaged = useCallback('.length, end + 1)}`)
    for (const [staged, intentId, cleared] of [
      [{ intentId: 'i-1', tool: 'solar-homeruns' }, null, true],
      [{ intentId: 'i-1', tool: 'solar-homeruns' }, 'i-1', true],
      [{ intentId: 'i-1', tool: 'solar-homeruns' }, 'i-2', false],
      [{ intentId: 'i-1', tool: 'solar-homeruns', confirmed: true }, null, false],
      [null, null, false],
    ]) {
      const ref = { current: staged }
      const pending = []
      clear(ref, (value) => pending.push(value))(intentId)
      assert.deepEqual(pending, cleared ? [null] : [])
      assert.equal(ref.current, cleared ? null : staged)
    }
    const dismiss = appSource.slice(appSource.indexOf('const onDismissSolarFlowRoute = useCallback'),
      appSource.indexOf('}, [clearSolarFlowStaged, dismissRoute])'))
    assert.ok(dismiss.indexOf('clearSolarFlowStaged()') >= 0 && dismiss.indexOf('dismissRoute()') > dismiss.indexOf('clearSolarFlowStaged()'))
    assert.ok(appSource.includes('onDismiss={ENV_SOLAR_FLOW_RAIL ? onDismissSolarFlowRoute : dismissRoute}'))
    assert.ok(appSource.includes('onDismissRoute: () => (ENV_SOLAR_FLOW_RAIL ? onDismissSolarFlowRoute() : dismissRoute()),'))
    const confirmStart = appSource.indexOf('const onConfirmCatalogRun = useCallback')
    const body = appSource.slice(confirmStart, appSource.indexOf('}, [dismissRoute, mock, onRun])', confirmStart))
    const fence = body.indexOf('if (ENV_SOLAR_FLOW_RAIL) {')
    const clears = [...body.matchAll(/if \(ENV_SOLAR_FLOW_RAIL\) clearSolarFlowStaged\(intent\?\.intentId \?\? null\)\r?\n\s*return\r?\n/g)]
    assert.equal(clears.length, 2)
    assert.ok(clears.every((match) => match.index < fence))
    const effect = appSource.slice(appSource.indexOf('const onDismissSolarFlowRoute = useCallback'))
    assert.ok(effect.includes("route?.runIntent?.intentId !== staged.intentId) clearSolarFlowStaged(staged.intentId)"))
  })

  it('FL15 App closes the step editor and Solar settings form on a flow switch', () => {
    const rail = appSource.indexOf('<SolarFlowRail')
    const props = appSource.slice(rail, appSource.indexOf('/>', rail))
    assert.ok(props.includes('onFlowChange={onSolarFlowChange}'))
    assert.ok(props.includes('onOpenStep={onOpenSolarFlowStep}'))
    assert.equal(appSource.split('onFlowChange').length - 1, 1)
    const change = appSource.indexOf('const onSolarFlowChange = useCallback((next) => {')
    assert.notEqual(change, -1)
    const end = appSource.indexOf('}, [onCloseSolarFlowStep])', change)
    assert.notEqual(end, -1)
    const body = appSource.slice(change, end)
    assert.ok(body.includes('setSolarFlow(solarFlowId(next))'))
    assert.match(body, /onCloseSolarFlowStep\(\)/)
    assert.match(body, /setSolarFormTool\(null\)/)
  })

  it('W20 mounts one live workspace container beside the rail under the same Solar fence', () => {
    const fence = appSource.indexOf(condition)
    const rail = appSource.indexOf('<SolarFlowRail', fence)
    const tools = appSource.indexOf('<SolarWorkspaceTools', rail)
    const editor = appSource.indexOf('{solarFlowEditor && (', rail)
    assert.ok(fence >= 0 && rail > fence && tools > rail && editor > tools)
    assert.equal(appSource.split('<SolarWorkspaceTools').length, 2)
    const host = appSource.slice(fence, tools)
    assert.ok(host.includes('<div className="solar-flow-host">'))
    assert.match(host, new RegExp('workspacePanelsByFlow=\\{\\s*!mock && catalogRunContext\\?\\.drawingId\\s*\\? SolarWorkspaceTools\\.workspacePanelsByFlow\\s*: undefined\\s*\\}'))
    assert.ok(host.slice(host.indexOf('/>', host.indexOf('<SolarFlowRail'))).includes('{!mock && ('))
    const props = appSource.slice(tools, appSource.indexOf('/>', tools))
    for (const binding of [
      'drawingId={catalogRunContext?.drawingId ?? null}',
      'projectId={catalogRunContext?.projectId ?? null}',
      'drawingVersion={catalogRunContext?.drawingVersion ?? null}',
      'flow={solarFlow}', 'checkoutHeld={heldByUs}', 'busy={!!running}',
      'getCheckoutCapability={() => checkoutCapabilityRef.current?.()}',
      'onPhysicalHeadChanged={() => loadCatalog()}',
      'onDrawingVersionChanged={seatCompletedVersion}',
    ]) assert.ok(props.includes(binding), binding)
  })

  it('GP5-17 live drawing rail receives the original guarded workspace registry', () => {
    const rail = appSource.indexOf('<SolarFlowRail')
    const props = appSource.slice(rail, appSource.indexOf('/>', rail))
    assert.match(props, new RegExp('workspacePanelsByFlow=\\{\\s*!mock && catalogRunContext\\?\\.drawingId\\s*\\? SolarWorkspaceTools\\.workspacePanelsByFlow\\s*: undefined\\s*\\}'))
    assert.equal(appSource.split('<SolarWorkspaceTools').length, 2)
  })

  it('W20 resets the selected flow when the rail mount predicate becomes false', () => {
    assert.ok(appSource.includes('const [solarFlow, setSolarFlow] = useState(DEFAULT_SOLAR_FLOW)'))
    assert.ok(appSource.includes("if (!(ENV_SOLAR_FLOW_RAIL && ENV_CAD_EDIT && ENV_SOLAR_SETTINGS_FORM && drafting && surfaceSlots.toolbar.profile === 'solar'))"))
    const start = appSource.indexOf("if (!(ENV_SOLAR_FLOW_RAIL && ENV_CAD_EDIT && ENV_SOLAR_SETTINGS_FORM && drafting")
    const reset = appSource.slice(start, appSource.indexOf('}, [drafting, surfaceSlots.toolbar.profile])', start))
    assert.ok(reset.includes('setSolarFlow(DEFAULT_SOLAR_FLOW)'))
  })

  it('SEAT wiring W1 wraps the form and flow hosts once before the ribbon', () => {
    const open = '<SolarFlowSeat seated={solarFlowSeated}>'
    const close = '</SolarFlowSeat>'
    assert.equal(appSource.split(open).length - 1, 1)
    assert.equal(appSource.split(close).length - 1, 1)
    const start = appSource.indexOf(open)
    const host = appSource.indexOf("{ENV_CAD_EDIT && drafting && surfaceSlots.toolbar.profile === 'solar' && solarFormTool && (")
    const editor = appSource.indexOf('{solarFlowEditor && (', host)
    const end = appSource.indexOf(close)
    assert.ok(start >= 0 && start < host && end > editor)
    assert.ok(end < appSource.indexOf('{/* W4c-V1: the drafting ribbon'))
    assert.ok(appSource.includes("import SolarFlowSeat from './solar/SolarFlowSeat.jsx'"))
  })

  it('SEAT wiring W2 uses the complete rail-first predicate and retains the single fence', () => {
    assert.ok(appSource.includes("const solarFlowSeated = ENV_SOLAR_FLOW_RAIL && ENV_CAD_EDIT && ENV_SOLAR_SETTINGS_FORM && drafting && surfaceSlots.toolbar.profile === 'solar'"))
    assert.equal(appSource.split(condition).length - 1, 1)
  })

  it('SEAT wiring W3 seats the panel beside the workbench and preserves Start and picking', () => {
    const css = readFileSync(new URL('./site/cockpit.css', import.meta.url), 'utf8').split('\r').join('')
    assert.equal(css.split('\n').find((line) => line.trim()), '.viewer-marquee {')
    const selector = '.studio-shell .app[data-surface="solar"] .solar-flow-seat {'
    const rules = []
    for (let start = css.indexOf(selector); start !== -1; start = css.indexOf(selector, start + selector.length)) {
      const end = css.indexOf('}', start)
      rules.push({ start, body: css.slice(start + selector.length, end) })
    }
    const fixedRules = rules.filter(({ body }) => body.includes('position: fixed'))
    assert.equal(fixedRules.length, 1)
    const { start, body: rule } = fixedRules[0]
    for (const value of ['position: fixed', 'left: calc(var(--ck-pane) + 458px)', 'top: calc(var(--ck-canvas-top) + 86px)']) assert.ok(rule.includes(value))
    const fixed = css.indexOf('top: calc(var(--ck-canvas-top) + 36px)')
    const workbench = css.lastIndexOf('.cad-edit-workbench {', fixed)
    assert.ok(workbench >= 0 && start > fixed && start < css.indexOf('/* ---- y 880-905: the command line'))
    assert.ok(css.includes('.studio-shell .app[data-start-open="true"] .solar-flow-seat { visibility: hidden; }'))
    assert.ok(css.includes('.studio-shell .app[data-surface="solar"] .workspace-card[data-cockpit-picking="1"] :is(.solar-flow-seat, .solar-flow-seat *) { pointer-events: none; }'))
    assert.ok(css.includes('.studio-shell .app[data-surface="solar"] .workspace-card[data-cockpit-picking="1"] .solar-flow-seat { opacity: 0.42; }'))
  })

  it('SEAT wiring W4 every seat rule sits inside a media block', () => {
    const source = readFileSync(new URL('./site/cockpit.css', import.meta.url), 'utf8')
    assert.deepEqual(seatRulePlacementFailures(source), [])
  })

  it('SEAT wiring W6 rejects misplaced Start rules while ignoring quoted braces', () => {
    const source = readFileSync(new URL('./site/cockpit.css', import.meta.url), 'utf8')
    const startRule = '.studio-shell .app[data-start-open="true"] .solar-flow-seat { visibility: hidden; }'
    assert.equal(source.split(startRule).length - 1, 1)
    const withoutStart = source.replace(startRule, '')
    const narrowHeader = '@media (max-width: 980px) {'
    assert.ok(withoutStart.includes(narrowHeader))
    const narrow = withoutStart.replace(narrowHeader, narrowHeader + '\n' + startRule)
    const spoofed = withoutStart + '\n.probe::before { content: ";@media (min-width: 981px) { .dummy {"; }\n'
      + startRule + '\n.probe::after { content: "}}"; }\n'
    const print = withoutStart + '\n@media print {\n' + startRule + '\n}\n'
    for (const [name, mutated] of [['narrow', narrow], ['quoted spoof', spoofed], ['print', print]]) {
      assert.ok(seatRulePlacementFailures(mutated).length > 0, name)
    }
    assert.deepEqual(seatRulePlacementFailures(source + '\n.probe::before { content: "{"; }\n'), [])
  })

  it('SEAT wiring W7 confines all seat material rules to desktop media', () => {
    const source = readFileSync(new URL('./solar/solarFlow.css', import.meta.url), 'utf8')
    const { occurrences, failures } = seatCssStructure(source)
    assert.deepEqual(failures, [])
    assert.ok(occurrences.length > 0, 'seat material rules must be present')
    for (const { index, headers } of occurrences) {
      assert.ok(headers.some((header) => header.startsWith('@media ') && header.includes('min-width: 981px')), 'seat material outside desktop media at ' + index)
    }
  })

  it('SEAT wiring W5 the overview yields in the narrow desktop band', () => {
    const css = readFileSync(new URL('./site/cockpit.css', import.meta.url), 'utf8').split('\r').join('')
    const mediaBlock = (query) => {
      const start = css.indexOf(query)
      assert.ok(start >= 0, query)
      const open = css.indexOf('{', start + query.length)
      assert.ok(open >= 0)
      let depth = 1
      let end = open + 1
      for (; end < css.length && depth > 0; end += 1) {
        if (css[end] === '{') depth += 1
        else if (css[end] === '}') depth -= 1
      }
      assert.equal(depth, 0)
      return css.slice(open + 1, end - 1)
    }
    const band = mediaBlock('@media (min-width: 981px) and (max-width: 1199px)')
    assert.ok(band.includes('.studio-shell .app[data-surface="solar"]:has(.solar-flow-seat):has(.properties-dock) .cad-overview { display: none; }'))
    const narrow = mediaBlock('@media (max-width: 980px)')
    assert.ok(narrow.includes('.studio-shell .app[data-surface="solar"] .solar-flow-seat { display: contents; }'))
  })
})

describe('J1 Browser composition', () => {
  const compiled = esbuild.transformSync(appSource, { loader: 'jsx' }).code
  const mount = (name) => {
    const match = compiled.match(new RegExp('React\\.createElement\\(\\s*' + name + ',\\s*'))
    assert.ok(match, name + ' must be mounted in executable App code')
    return compiled.slice(match.index, match.index + 1800)
  }

  it('J1 row4 signed-in first run mounts the existing workspace controller entry', () => {
    assert.match(compiled, /!mock && signedIn && !openProjectId &&.*React\.createElement\(\s*ProjectStartPanel,/s)
    const start = mount('ProjectStartPanel')
    for (const [prop, binding] of Object.entries({
      bootstrapState: 'workspaceController.bootstrapState', projects: 'projects',
      projectsLoaded: 'workspaceController.projectsLoaded', projectsLoading: 'projectsLoading',
      openProjectId: 'openProjectId', projectsError: 'projectsErr', orgBusy: 'orgBusy',
      projectBusy: 'projectBusy', orgDraftError: 'workspaceController.orgDraftError',
      projectDraftError: 'workspaceController.projectDraftError', orgConflict: 'workspaceController.orgConflict',
      onCreateOrg: 'createWorkspaceOrg', onCreateProject: 'createWorkspaceProject',
      onOpenProject: 'onOpenProject', onLoadProjects: 'workspaceController.loadProjects',
    })) {
      // Controller fields are destructured in App; esbuild prints same-name props as shorthand.
      const emitted = prop === binding ? prop : `${prop}: ${binding}`
      assert.ok(start.split('\n').some((line) => line.trim().replace(/,$/, '') === emitted), `${prop} must reach the existing controller`)
    }
    assert.match(start, /drawingMounted:\s*Boolean\(shown\)/)
    assert.equal((compiled.match(/= useWorkspaceController\(/g) || []).length, 1)
    assert.match(compiled, /openProject:\s*onOpenProject/)
    assert.match(compiled, /createProject:\s*createWorkspaceProject/)
    assert.match(compiled, /createOrg:\s*createWorkspaceOrg/)
  })

  it('J1 row5 the mounted material intake binds the open project before upload', () => {
    const live = mount('LiveProjectMaterialIntake')
    assert.match(live, /key:\s*openProjectId/)
    assert.match(live, /project_id:\s*openProjectId/)
    assert.match(live, /onAttached:\s*rehydrate/)
    assert.match(live, /artifacts:\s*workspace\?\.drawing_artifacts \|\| \[\]/)
    assert.equal((compiled.match(/= useDrawingUploadController\(/g) || []).length, 2)
    assert.equal((compiled.match(/= useMaterialIntake\(/g) || []).length, 1)

    // Execute only the mounted adapter, with the two IO controllers replaced.
    // This pins ordering and refusal without making an upload or a project write.
    const begin = appSource.indexOf('function LiveProjectMaterialIntake(')
    const end = appSource.indexOf('export default function App()', begin)
    assert.ok(begin >= 0 && end > begin)
    const adapter = esbuild.transformSync(appSource.slice(begin, end), { loader: 'jsx' }).code
    const events = []
    const project = { project_id: 'project-j1', name: 'J1 roof' }
    const artifacts = [{ drawing_id: 'drawing-j1' }]
    const file = { name: 'roof.dxf' }
    const upload = { actions: { upload: (value) => events.push(['upload', value]) } }
    let allowed = true
    const intake = { begin: (target) => { events.push(['begin', target]); return allowed }, retry: () => {} }
    const onAttached = () => {}
    const renderAdapter = new Function('React', 'ProjectMaterialIntake', 'useDrawingUploadController', 'useMaterialIntake',
      adapter + '\nreturn LiveProjectMaterialIntake')(
      { createElement: (type, props) => ({ type, props }) }, 'ProjectMaterialIntake', () => upload,
      (options) => { assert.equal(options.upload, upload); assert.equal(options.onAttached, onAttached); return intake },
    )
    const element = renderAdapter({ project, artifacts, onAttached })
    assert.equal(element.type, 'ProjectMaterialIntake')
    assert.equal(element.props.project, project)
    assert.equal(element.props.upload, upload)
    assert.equal(element.props.intake, intake)
    assert.equal(element.props.artifacts, artifacts)
    element.props.onStartUpload(file)
    assert.deepEqual(events, [['begin', { projectId: 'project-j1', projectName: 'J1 roof', fileName: 'roof.dxf' }], ['upload', file]])
    events.length = 0
    allowed = false
    element.props.onStartUpload(file)
    assert.equal(events.length, 1)
    assert.equal(events[0][0], 'begin')
  })

  it('J1 row6 demo material mounts the disabled component without an IO controller', () => {
    const source = codeOnly(appSource)
    assert.ok(source.includes("projectPane === 'material' && !standaloneMounted && (mock || !signedIn || !openProjectId"))
    assert.ok(source.includes('? <ProjectMaterialIntake project={null} mock={mock} artifacts={[]} />'))
    assert.ok(source.includes(': <LiveProjectMaterialIntake'))
    const appBody = source.slice(source.indexOf('export default function App()'))
    assert.doesNotMatch(appBody, /useDrawingUploadController\(/)
    assert.doesNotMatch(appBody, /useMaterialIntake\(/)
    mount('ProjectMaterialIntake')
    const standalone = mount('StandaloneMaterialUpload')
    assert.match(standalone, /token:\s*standaloneToken/)
    assert.match(standalone, /onReady:\s*onStandaloneReady/)
    assert.ok(source.includes("surfaceSlots.ground === 'board' && !mock && !openProjectId"))
    assert.ok(source.includes("pane={standaloneMounted && projectPane === 'material' ? null : projectPane}"))
    assert.ok(source.includes('PROFILE_REASONS.uploadDrawing'))
  })

  it('CSU wiring: the ribbon memo lists every value the Upload drawing action reads', () => {
    assert.deepEqual(csuUploadProblems(appSource), [])
  })

  it('CSU wiring: the memo binding refuses a decoy memo, a string action and a member read', () => {
    const action = "files: { onUpload: !mock && (openProjectId ? signedIn : standalonePolicyReady) ? go : null }"
    const all = '[mock, openProjectId, signedIn, standalonePolicyReady]'
    const memo = (name, body, deps) => `const ${name} = useMemo(() => ({ ${body} }), ${deps})\n`
    assert.deepEqual(csuUploadProblems(memo('profileTabs', action, all)), [])
    assert.deepEqual(csuUploadProblems(memo('decoy', action, all) + memo('profileTabs', action, '[mock, openProjectId, signedIn]')),
      ['standalonePolicyReady must be a dependency of the ribbon memo'])
    assert.equal(csuUploadProblems(memo('profileTabs', "files: { onUpload: 'mock openProjectId signedIn standalonePolicyReady' }", all)).length, 4)
    assert.deepEqual(csuUploadProblems(memo('profileTabs', action.replace('!mock', '!window.mock'), all)),
      ['mock is not read by the Upload drawing action'])
    assert.deepEqual(csuUploadProblems(memo('profileTabs', `${action}, again: { ${action} }`, all)),
      ['expected one files.onUpload inside the profileTabs memo, found 2'])
  })

  // Both placements of a project panel, in source order: [inline, board].
  // Props are read from the compiled object literal, so a comment cannot satisfy them.
  const placements = (name) => {
    const found = [...compiled.matchAll(new RegExp('React\\.createElement\\(\\s*' + name + ',\\s*\\{([^{}]*)\\}', 'g'))]
    assert.equal(found.length, 2, name + ' must be created exactly twice (inline and on the board)')
    return found.map((match) => ({
      index: match.index,
      guard: compiled.slice(Math.max(0, match.index - 200), match.index),
      props: match[1].split(',').map((entry) => entry.trim()).filter(Boolean),
    }))
  }
  const PURE = '(?:\\/\\*\\s*@__PURE__\\s*\\*\\/\\s*)?$'

  it('J1B row1: the Browser board hosts the campaign and summary panels', () => {
    assert.match(compiled, /const boardHostsProject = boardVisible && surfaceSlots\.ground === "board";/)
    const grounds = compiled.search(/React\.createElement\(\s*SurfaceGrounds,/)
    const panelSlot = compiled.indexOf('panel: surfaceSlots.ground === "board" ?', grounds)
    const intakeAt = compiled.slice(panelSlot).search(/React\.createElement\(\s*LiveProjectMaterialIntake,/)
    const intake = panelSlot + intakeAt
    assert.ok(grounds >= 0 && panelSlot > grounds && intakeAt > 0, 'the board panel slot must exist')
    for (const name of ['CampaignPanel', 'WorkspaceSummary']) {
      const [inline, board] = placements(name)
      assert.ok(inline.index < grounds, name + ' inline placement must precede the grounds')
      assert.match(inline.guard, new RegExp('!boardHostsProject\\s*&&\\s*!mock\\s*&&\\s*openProjectId\\s*&&\\s*' + PURE),
        name + ' inline placement must be guarded by !boardHostsProject')
      assert.ok(board.index > intake, name + ' board placement must follow the pane content in the panel slot')
      assert.match(board.guard, new RegExp('!mock\\s*&&\\s*openProjectId\\s*&&\\s*' + PURE), name + ' board placement keeps its guards')
      assert.doesNotMatch(board.guard, /boardHostsProject/)
    }
  })

  it('J1B row2: the board placement keeps every prop', () => {
    const names = (props) => props.map((entry) => entry.split(':')[0].trim()).sort()
    for (const [name, expected] of Object.entries({
      CampaignPanel: ['projectId', 'projectName', 'signedIn', 'authorityProvider'],
      WorkspaceSummary: ['workspace', 'loading', 'selectedVersionId', 'onSelectVersion', 'onClose'],
    })) {
      const [inline, board] = placements(name)
      assert.deepEqual(names(inline.props), [...expected].sort(), name + ' inline props')
      assert.deepEqual(names(board.props), names(inline.props), name + ' board placement must pass the same props')
      assert.deepEqual([...board.props].sort(), [...inline.props].sort(), name + ' board placement must bind the same values')
    }
  })
})

function codeOnly(src) {
  const chars = src.split('')
  let i = 0
  while (i < src.length) {
    const quote = src[i]
    if (quote === "'" || quote === '"' || quote === '`') {
      i++
      while (i < src.length) {
        if (src[i] === '\\') i += 2
        else if (src[i++] === quote) break
      }
    } else if (src[i] === '/' && src[i + 1] === '/') {
      i += 2
      while (i < src.length && src[i] !== '\n' && src[i] !== '\r') chars[i++] = ' '
    } else if (src[i] === '/' && src[i + 1] === '*') {
      i += 2
      while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) {
        if (src[i] !== '\n' && src[i] !== '\r') chars[i] = ' '
        i++
      }
      i += 2
    } else {
      i++
    }
  }
  return chars.join('')
}

describe('C-05 ship context wiring', () => {
  // J2 replaces the old hardcoded-null pins with the mounted controller projection.
  const start = appSource.indexOf('  const ship = useMemo(')
  const end = appSource.indexOf('  // Readiness follows', start)
  const projection = appSource.slice(start, end)
  const project = (iosShipController, iosContract = null, shipControllerLive = true) => new Function(
    'iosShipController', 'iosContract', 'canonicalVersionId', 'useMemo', 'shipLaunchReason', 'shipErrorSentence', 'document', 'shipControllerLive',
    projection + '\nreturn ship',
  )(iosShipController, iosContract, 'revision-j2', (fn) => fn(), () => 'Setup required.', (error) => error, {
    querySelector: () => null,
  }, shipControllerLive)

  it('J2 row1 mounts one controller and exposes its real launch only when ready', () => {
    const compiled = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    assert.equal((compiled.match(/= useIosShipController\(/g) || []).length, 1)
    assert.match(compiled, /projectId: openProjectId/)
    assert.match(compiled, /sessionActive: !mock && signedIn/)
    assert.match(compiled, /enabled: shipControllerLive/)
    assert.match(compiled, /const shipControllerLive = ENV_IOS_SURFACE && surfaceSlots.toolbar.profile === "ship" && !mock && signedIn/)
    assert.match(compiled, /tenantKey: tenant \|\| config.tenant/)
    const launch = () => 'launched'
    assert.equal(project({ phase: 'ready', launch }).onLaunch, launch)
    assert.match(compiled, /profileRibbonTabs\([\s\S]*?\bship\s*[,}]/)
    assert.match(compiled, /onLaunch: ship.onLaunch/)
  })

  it('J2 row16 the ship record carries explicit liveness even at idle', () => {
    assert.equal(project({ phase: 'idle' }, null, false).controllerLive, false)
    assert.equal(project({ phase: 'idle' }, null, true).controllerLive, true)
  })

  it('J2 row2 every non-ready controller phase has an absent launch handler', () => {
    for (const phase of ['idle', 'loading', 'launching', 'running', 'succeeded', 'failed', 'setup-required', 'unavailable']) {
      assert.equal(project({ phase, launch: () => {} }).onLaunch, null)
    }
  })

  it('J2 row8 readiness and launch use the same controller phase', () => {
    assert.match(appSource, /iosReady: iosShipController.phase === 'ready'/)
    assert.doesNotMatch(appSource, /iosReady:.*iosContract/)
    for (const phase of ['ready', 'loading', 'running', 'succeeded', 'failed', 'setup-required', 'unavailable', 'idle']) {
      assert.equal(typeof project({ phase, launch: () => {} }).onLaunch === 'function', phase === 'ready')
    }
  })

  it('C-05 row10 the ship receipt handler opens the real details disclosure', () => {
    assert.match(codeOnly(projection), /onReceipts: iosContract\?\.receipt_id \? \(\) => \{/)
    assert.ok(projection.includes("document.querySelector('.studio-profile-info details')"))
    assert.ok(projection.includes("details.open = true; details.querySelector('summary')?.focus()"))
    assert.equal(typeof project({ phase: 'ready' }, { receipt_id: 'receipt' }).onReceipts, 'function')
    assert.equal(project({ phase: 'ready' }).onReceipts, null)
  })

  it('C-05 row11 ignores a commented handler and preserves literal slashes', () => {
    assert.doesNotMatch(codeOnly('/* onLaunch: null */'), /onLaunch/)
    const literals = "const string = '//not-a-comment'; const template = `//not-a-comment`"
    assert.equal(codeOnly(literals), literals)
  })

  it('C-05 row8 consumes the existing contract and canonical revision', () => {
    const contract = { receipt_id: 'receipt' }
    assert.equal(project({ phase: 'ready' }, contract).contract, contract)
    assert.equal(project({ phase: 'ready' }, contract).revision, 'revision-j2')
  })
})
describe('C-04C Solar shown-document readiness', () => {
  const start = appSource.indexOf('  const solarReady =')
  const end = appSource.indexOf('  const surfaceStates =', start)
  const derivation = appSource.slice(start, end)
  const ready = (activeIntake, engineDocument = null, profile = 'solar', solarStarter = 'idle') => {
    assert.ok(start >= 0 && end > start)
    const surfaceSlots = { toolbar: { profile } }
    return new Function('activeIntake', 'engineDocument', 'surfaceSlots', 'solarStarter',
      `${derivation}\nreturn solarReady`)(activeIntake, engineDocument, surfaceSlots, solarStarter)
  }

  it('C-04C row5 opener open without onShown remains not ready', () => {
    const document = { documentId: 'solar-starter.dxf', documentOrigin: 'starter' }
    for (const state of ['idle', 'opening', 'open', 'failed']) assert.equal(ready(null, document, 'solar', state), false)
    assert.doesNotMatch(derivation, /solarStarter|hasDrawing|session\.engineParsed/)
    assert.match(appSource, new RegExp(String.raw`productSurfaceStates\(\{[\s\S]*?\bsolarReady,\s*\}\), \[mock, signedOut, shown, health, iosShipController.phase, solarReady\]`))
    assert.match(appSource, new RegExp(String.raw`onShown=\{\(intake, history\) => \{\s*setActiveIntake\(intake\)`))
  })

  it('C-04C row6 shown starter requires surface, shown intake, matching document and load provenance', () => {
    const intake = { documentId: 'solar-starter.dxf' }
    const document = { ...intake, documentOrigin: 'starter' }
    assert.equal(ready(intake, document), true)
    assert.equal(ready(intake, document, 'cad'), false)
    assert.equal(ready(intake, document), true)
    assert.equal(ready(null, document), false)
    assert.equal(ready(intake), false)
    assert.equal(ready(intake, { ...document, documentId: 'other.dxf' }), false)
    assert.equal(ready(intake, { ...document, documentOrigin: null }), false)
    assert.match(derivation, new RegExp(String.raw`surfaceSlots\.toolbar\.profile === 'solar'`))
    assert.doesNotMatch(derivation, /activeSurface/)
    assert.match(derivation, /!!activeIntake/)
    assert.ok(derivation.includes('engineDocument?.documentId === activeIntake.documentId'))
    assert.ok(derivation.includes("engineDocument.documentOrigin === 'starter'"))
    assert.ok(derivation.includes("engineDocument.documentOrigin === 'head'"))
    assert.doesNotMatch(derivation, /SOLAR_STARTER_DOCUMENT_ID|headDocumentId|shownHeadVersion/)
  })

  it('C-04C row7 hand imports including reserved starter and head names are not Ready', () => {
    for (const documentId of ['roof.dxf', 'other-v1.dxf', 'solar-starter.dxf', 'demo-v1.dxf']) {
      assert.equal(ready({ documentId }, { documentId, documentOrigin: 'import' }), false)
    }
  })

  it('C-04C row8 the displayed demo head makes Solar ready at any version', () => {
    for (const documentId of ['demo-v1.dxf', 'demo-v12.dxf']) {
      const document = { documentId, documentOrigin: 'head' }
      assert.equal(ready({ documentId }, document), true)
      assert.equal(ready({ documentId }, document, 'cad'), false)
    }
  })
})

describe('C-04B Solar ribbon wiring', () => {
  it('row9 gates solved routes on mock rooftop identity, preview, head and dirty engine', () => {
    assert.match(appSource, /solarStringsEligible = !!studioGround && surfaceSlots\.groundMaterial\.solarStrings && mock\s+&& !isEditFixture && DRAWING_SOURCE === 'rooftop_demo' && intakeIsRooftopSample/)
    assert.match(appSource, new RegExp(String.raw`solarRouteStatus\(\{\s+eligible: solarStringsEligible, previewing, head: drawingState\?\.head \?\? 1,\s+engineDirty,`))
    assert.match(appSource, new RegExp(String.raw`solarRouteDisplay\(\{\s+status: solarRoutesStatus, shown: showSolarStrings, routes: demoSolveRoutes`))
    assert.ok(appSource.includes('selectedHandle, onClearSelection: () => setSelectedHandle(null)'))
  })
  it('row16 binds routes to the displayed document and records solve failure or emptiness', () => {
    const assertRouteBinding = (source) => {
      const block = source.match(new RegExp(String.raw`solarRouteStatus\(\{([\s\S]*?)\}\)`))?.[1]
      assert.ok(block, 'the route status call exists')
      for (const line of block.split(/\r?\n/)) {
        assert.doesNotMatch(line, /^\s*\/\//, 'route status inputs must be executable')
      }
      assert.doesNotMatch(block, /\/\*|\*\//, 'route status inputs must not be block comments')
      assert.match(block, /^\s*documentId: activeIntake\?\.documentId \?\? null,\s*$/m)
      assert.match(block, /^\s*committedVersion: engineDocument\?\.committedVersion \?\? null,\s*$/m)
      assert.ok(block.includes('headDocumentId: `${REQUESTED_DRAWING_ID}-v1.dxf`'))
    }
    assertRouteBinding(appSource)
    const commented = appSource.replace('    documentId: activeIntake?.documentId ?? null,', '    // documentId: activeIntake?.documentId ?? null,')
    assert.notEqual(commented, appSource, 'the comment mutation must apply')
    assert.throws(() => assertRouteBinding(commented))
    assert.ok(appSource.includes('const [engineDocument, setEngineDocument] = useState(null)'))
    assert.match(stripped, /onDocumentChange:\s*setEngineDocument/)
    assert.equal(appSource.includes('allowedDocumentIds'), false)
    assert.ok(appSource.includes("const [demoSolveState, setDemoSolveState] = useState('pending')"))
    assert.match(appSource, new RegExp(String.raw`\.catch\(\(\) => \{\s+if \(live\) setDemoSolveState\('failed'\)`))
    assert.ok(appSource.includes("setDemoSolveState(routes.length ? 'loaded' : 'empty')"))
    assert.ok(appSource.includes('solve: demoSolveState, routes: demoSolveRoutes'))
    assert.ok(appSource.includes('solar: { status: solarRoutesStatus, shown: showSolarStrings'))
  })
  it('row12 profile entry changes only the selected ribbon tab', () => {
    assert.ok(appSource.includes('profileEntryTab(previousRibbonProfile.current, surfaceSlots.toolbar.profile, ribbonTab, surfaceSlots.toolbar.home)'))
    const start = appSource.indexOf('  const previousRibbonProfile = useRef(null)')
    const end = appSource.indexOf('  const ribbon = useMemo', start)
    const entryRule = appSource.slice(start, end)
    assert.match(entryRule, /previousRibbonProfile.current = surfaceSlots.toolbar.profile/)
    assert.match(entryRule, /setRibbonTab\(activeRibbonTab\)/)
    assert.doesNotMatch(entryRule, /reset|setView|undo|openBytes|openFile/)
    assert.ok(appSource.includes("activeRibbonTab === 'solar' ? ['solar-panels']"))
    assert.ok(appSource.includes('id="cockpit-solar-panels-slot"'))
  })
})

describe('Solar rooftop starter', () => {
  it('mounts SolarStarterOpener only for a live empty Solar workspace', () => {
    assert.match(appSource, /<SolarStarterOpener\s+enabled=\{!mock && drawingLoad.drawingId === REQUESTED_DRAWING_ID && drawingLoad.state === 'absent' && surfaceSlots\.toolbar\.profile === 'solar'\}\s+fetchDxf=\{fetchSampleDxf\}/)
  })
  it('row13 bootstraps only the Viewer with the empty starter intake', () => {
    assert.match(appSource, /\(intake \|\| solarStarter === 'open'\) &&/)
    assert.match(appSource, /<Viewer\s[\s\S]*?intake=\{intake \?\? SOLAR_STARTER_EMPTY_INTAKE\}/)
    assert.match(appSource, /ref=\{intake \? viewerRef : solarStarterViewerRef\}/)
  })
  it('row16 drawingLoad distinguishes pending, seated, absent and failed session loads', () => {
    assert.ok(appSource.includes("useState({ drawingId: REQUESTED_DRAWING_ID, state: 'pending' })"))
    assert.ok(appSource.includes('resetDrawing(); setLoadErr(null)'))
    assert.match(appSource, /if \(!hasDrawingSelection\([\s\S]*?setDrawingLoad\(\{ drawingId: null, state: 'idle' \}\)[\s\S]*?return \(\) => \{ alive = false \}/)
    assert.ok(appSource.includes("setDrawingLoad({ drawingId: loadDrawingId, state: 'pending' })"))
    assert.ok(appSource.includes("setDrawingLoad({ drawingId: loadDrawingId, state: d != null ? 'seated' : 'absent' })"))
    assert.ok(appSource.includes("setDrawingLoad({ drawingId: loadDrawingId, state: e?.status === 404 ? 'absent' : 'failed', failure: classifyDrawingLoadFailure(e) })"))
  })
  it('row16 permanent drawing load failures explain the failure and offer Retry only for non-permanent failures', () => {
    assert.ok(appSource.includes("import { classifyDrawingLoadFailure } from './drawing/loadFailure.js'"))
    const start = appSource.indexOf('{loadErr && !signedOut && (')
    const end = appSource.indexOf('{signedOut &&', start)
    assert.ok(start >= 0 && end > start, 'drawing load failure block exists')
    const failedLoad = appSource.slice(start, end)
    assert.ok(failedLoad.includes("<span className=\"pane-fail-reason\">{drawingLoad.failure === 'permanent' ? 'This drawing could not be found or opened.' : loadErr}</span>"))
    assert.match(failedLoad, new RegExp(String.raw`\{drawingLoad\.failure !== 'permanent' && \(\s*<button className="chip-act" onClick=\{\(\) => setIntakeRetryKey\(\(k\) => k \+ 1\)\}>Retry</button>\s*\)\}`))
    assert.equal(failedLoad.split('>Retry</button>').length - 1, 1, 'the failed-load Retry button appears only in the non-permanent conditional')
  })
  it('row19 pins drawing identity and drops superseded loader replies before cleanup', async () => {
    assert.ok(appSource.includes('requestedDrawingIdRef.current = REQUESTED_DRAWING_ID'))
    assert.match(appSource, /alive\s*&& loadDrawingId === requestedDrawingIdRef.current\s*&& isScopeCurrent\(\)/)
    assert.match(appSource, /\[mock, isEditFixture, intakeRetryKey, REQUESTED_DRAWING_ID, DRAWING_SOURCE,/)
    assert.equal(appSource.split("drawingLoad.drawingId === REQUESTED_DRAWING_ID && drawingLoad.state === 'absent'").length - 1, 2)
    assert.ok(appSource.includes("drawingSeated={drawingLoad.drawingId === REQUESTED_DRAWING_ID && drawingLoad.state === 'seated'}"))
    assert.ok(appSource.includes('consoleIntake={intake}'))
    const start = appSource.indexOf('    let alive = true', appSource.indexOf('// load session (intake'))
    const end = appSource.indexOf('  }, [mock, isEditFixture, intakeRetryKey', start)
    assert.ok(start > 0 && end > start)
    const body = appSource.slice(start, end)
    for (const result of ['success', 'absent', 'failed']) {
      const loads = [], seats = [], requests = []
      const requestedDrawingIdRef = { current: 'A' }
      const noop = () => {}
      const context = {
        REQUESTED_DRAWING_ID: 'A', DRAWING_SOURCE: 'A', requestedDrawingIdRef,
        isScopeCurrent: () => true, hasDrawingSelection,
        mock: false, isEditFixture: false, resetDrawing: noop,
        setDrawingLoad: (value) => loads.push(value), setLoadErr: noop,
        resetCatalogTransient: noop, clearToast: noop, setDrawer: noop,
        setTenant: noop, setTier: noop, setOrg: noop, clearAgentSession: noop,
        mockVersions: { reset: noop }, seatIntake: (value) => seats.push(value),
        sessionActions: { checking: noop, activate: noop },
        getSession: (_, source) => new Promise((resolve, reject) => requests.push({ source, resolve, reject })),
        getDrawingVersions: async () => ({ head: 1 }), adoptOrgId: noop,
        humanizeError: () => 'failed', is401: () => false,
      }
      const run = () => new Function(...Object.keys(context), body)(...Object.values(context))
      const cleanA = run()
      requestedDrawingIdRef.current = 'B'
      context.REQUESTED_DRAWING_ID = 'B'
      context.DRAWING_SOURCE = 'B'
      const cleanB = run()
      if (result === 'success') requests[0].resolve({ intake: { dwg: 'A' } })
      else requests[0].reject({ status: result === 'absent' ? 404 : 503 })
      await new Promise((done) => setImmediate(done))
      assert.deepEqual(loads, [{ drawingId: 'A', state: 'pending' }, { drawingId: 'B', state: 'pending' }])
      assert.deepEqual(seats, [])
      assert.deepEqual(requests.map((request) => request.source), ['A', 'B'])
      requests[1].resolve({ intake: { dwg: 'B' } })
      await new Promise((done) => setImmediate(done))
      assert.deepEqual(loads.at(-1), { drawingId: 'B', state: 'seated' })
      assert.deepEqual(seats, [{ dwg: 'B' }])
      cleanA(); cleanB()
    }
  })
})

describe('one-shell profile band', () => {
  it('mounts one band from the shell contract and retains unavailable quick actions', () => {
    assert.equal((appSource.match(/<CockpitTopBand\b/g) || []).length, 1)
    assert.match(appSource, /studioShell && surfaceSlots\.toolbar\.ribbon && \(\s*<CockpitTopBand/)
    assert.match(appSource, /const WORKSPACE_QUICK_BEFORE = Object\.freeze\(/)
    assert.match(appSource, /const WORKSPACE_QUICK_AFTER = Object\.freeze\(/)
    for (const id of ['quick-import-dxf', 'quick-save-version', 'quick-undo-edit', 'quick-redo-edit', 'quick-undo', 'quick-redo']) {
      assert.ok(appSource.includes(`id: '${id}'`), `missing quick action ${id}`)
    }
  })
})

describe('W4g S08-c: the Details drawer carries sanitized diagnostics', () => {
  it('imports the diagnostics composer and request failure collector', () => {
    assert.match(appSource, new RegExp("import \\{[^}]*\\bcomposeDiagnostics\\b[^}]*\\} from './diagnostics\\.js'"))
    assert.match(appSource, new RegExp("import \\{[^}]*\\brecentRequestFailures\\b[^}]*\\} from './api\\.js'"))
  })

  it('includes served identity and sanitized diagnostics in session details', () => {
    const start = appSource.indexOf('const openSessionDetails = useCallback(')
    assert.notEqual(start, -1)
    const end = appSource.indexOf('}, [', start)
    assert.notEqual(end, -1)
    const body = appSource.slice(start, end)
    for (const literal of ['served ', 'taskRevisionOf(', 'diagnostics: composeDiagnostics({', 'collectRefusals(document)']) {
      assert.ok(body.includes(literal), `missing session diagnostics: ${literal}`)
    }
  })

  it('passes checkout failure metadata to the banner and exposes the build on Details hover', () => {
    const checkout = appSource.match(/<CheckoutControls\s[\s\S]*?\/>/)?.[0]
    assert.ok(checkout)
    assert.ok(checkout.includes('failure={checkout.failure}'))
    const details = appSource.match(new RegExp('<button[^>]*onClick=\\{openSessionDetails\\}[^>]*>Details</button>'))?.[0]
    assert.ok(details)
    assert.ok(details.includes('title={`Session details · build ${__BUILD_HASH__}`}'))
  })
})

describe('W4g S08-a: an explicit demo starts in mock', () => {
  it('gates registry and skills discovery on mock', () => {
    const start = appSource.indexOf('const [catalogSkills, setCatalogSkills]')
    const end = appSource.indexOf('}, [mock])', start)
    assert.ok(start >= 0 && end > start)
    const effect = appSource.slice(start, end + '}, [mock])'.length)
    const guard = effect.indexOf('if (mock)')
    assert.ok(guard >= 0 && guard < effect.indexOf('fetchRegistry('))
    assert.ok(guard < effect.indexOf('fetchSkills('))
    assert.ok(effect.endsWith('}, [mock])'))
  })
  it('gates PromptBox MCP discovery on mock', () => {
    const promptBox = appSource.match(/<PromptBox\s[\s\S]*?\/>/)?.[0]
    assert.ok(promptBox)
    assert.ok(promptBox.includes('mcpDiscoveryEnabled={!mock}'))
  })
  it('decides explicit demo mode in the lazy initial state', () => {
    assert.match(appSource, new RegExp('useState\\(\\(\\) => config\\.mockDefault[\\s\\S]{0,200}explicitDemo\\(\\{'))
    assert.match(appSource, new RegExp("import \\{[^}]*\\bexplicitDemo\\b[^}]*\\} from './demoState\\.js'"))
  })
  it('renders the operator probe once and only outside mock', () => {
    assert.equal(appSource.split('<OperatorEntry />').length - 1, 1)
    assert.ok(appSource.split('\n').some((line) => line.includes('{!mock && <OperatorEntry />}')))
  })
  it('preserves the live session transitions and the 401 auto-demo hatch', () => {
    for (const literal of [
      'if (!mock) sessionActions.checking()',
      'if (!mock) sessionActions.activate({ tenant: t, tier: ti, org: o })',
      'if (!mock && is401(e)) {',
      'if (shouldAutoDemo({ authRequired: true, authConfigured, mock, signedIn: isSignedIn() })) setMock(true)',
    ]) assert.ok(appSource.includes(literal), `missing session contract: ${literal}`)
  })
})

describe('W4g bleed-2b: profile presentation preserves the engine document', () => {
  it('mounts the head opener after the engine document under its own studio and engine gates', () => {
    const ribbonEnd = appSource.indexOf('</DraftingRibbon>')
    const engine = appSource.indexOf('<EngineDocumentView')
    const opener = appSource.indexOf('<EngineHeadOpener')
    const guard = appSource.lastIndexOf('{ENV_CAD_EDIT && studioGround && (', opener)
    assert.equal(appSource.split('<EngineHeadOpener').length - 1, 1)
    assert.ok(ribbonEnd >= 0 && engine >= 0)
    assert.ok(opener > ribbonEnd && opener > engine)
    assert.ok(guard >= 0)
    assert.doesNotMatch(appSource.slice(guard, opener), /</)
  })
  it('mounts the engine document after the drafting ribbon under the studio and engine gates', () => {
    const ribbonStart = appSource.indexOf('{studioShell && surfaceSlots.toolbar.ribbon && studioRibbonHost && createPortal(')
    const ribbonEnd = appSource.indexOf('</DraftingRibbon>', ribbonStart)
    const engine = appSource.indexOf('<EngineDocumentView')
    assert.ok(ribbonStart >= 0 && ribbonEnd > ribbonStart)
    assert.ok(engine > ribbonEnd)
    assert.match(appSource.slice(ribbonEnd, engine), new RegExp('\\)\\}\\s*[\\s\\S]*?\\{ENV_CAD_EDIT && studioGround && \\(\\s*$'))
    assert.equal(appSource.split('<EngineDocumentView').length - 1, 1)
  })
  it('closes Start before requesting a profile and commits the URL with the state', () => {
    const selectStart = appSource.indexOf('const onSelectSurface =')
    const selectEnd = appSource.indexOf('[returnToDrawing, request])', selectStart)
    assert.ok(selectStart >= 0 && selectEnd > selectStart)
    const select = appSource.slice(selectStart, selectEnd)
    assert.match(select, /returnToDrawing\(\)\s+request\(id\)/)
    assert.doesNotMatch(select, /setActiveSurface|replaceState/)
    const commitStart = appSource.indexOf('const onCommit =')
    const commitEnd = appSource.indexOf('}, [])', commitStart)
    assert.ok(commitStart >= 0 && commitEnd > commitStart)
    const commit = appSource.slice(commitStart, commitEnd)
    assert.match(commit, /setActiveSurface\(id\)/)
    assert.match(commit, /searchForProductSurface\(window\.location\.search, id\)/)
    assert.match(commit, /window\.history\.replaceState/)
    assert.match(appSource, /committed: activeSurface, onCommit, isDrafting: groundShowsDrawing/)
  })
  it('settles presentation at drawing actions and exposes phases only in the studio', () => {
    assert.match(appSource, new RegExp('<EngineSessionProvider[^>]+onBeforeEdit=\\{closeStartForChange\\}\\s+onBeforeArm=\\{onBeforeArm\\}'))
    assert.match(appSource, new RegExp('const \\{ phase, request, settle, exitPending \\} = useStudioTransition\\('))
    const clickStart = appSource.indexOf('onClickCapture=')
    const clickEnd = appSource.indexOf('onChangeCapture=', clickStart)
    assert.ok(clickStart >= 0 && clickEnd > clickStart)
    const click = appSource.slice(clickStart, clickEnd)
    assert.match(click, new RegExp('if \\(exitPending\\(\\)\\) \\{\\s*event\\.preventDefault\\(\\);\\s*event\\.stopPropagation\\(\\);\\s*return\\s*\\}\\s*settle\\(\\)\\s+returnToDrawing\\(\\)'))
    assert.match(click, /settle\(\)\s+returnToDrawing\(\)/)
    const armStart = appSource.indexOf('const onBeforeArm = useCallback(')
    const armEnd = appSource.indexOf('}, [', armStart)
    assert.ok(armStart >= 0 && armEnd > armStart)
    assert.match(appSource.slice(armStart, armEnd), /if \(exitPending\(\)\) return false\s+settle\(\)\s+return true/)
    const start = appSource.indexOf('const closeStartForChange =')
    const end = appSource.indexOf('const onReturnToDrawing =', start)
    assert.match(appSource.slice(start, end), /settle\(\)/)
    assert.match(appSource, new RegExp("data-studio-transition=\\{studioGround && phase !== 'idle' \\? phase : undefined\\}"))
    assert.match(appSource, /const effectiveGround = studioGround \? \(boardVisible \? 'board' : surfaceGround\(activeSurface\)\) : null/)
    assert.match(appSource, /const leavingGround = useLeavingGround\(effectiveGround\)/)
    assert.match(appSource, new RegExp('leavingGround=\\{leavingGround\\}'))
    const viewer = appSource.slice(appSource.indexOf('? createPortal(<div className="studio-ground-viewer"'), appSource.indexOf(': viewerEl', appSource.indexOf('? createPortal(<div className="studio-ground-viewer"')))
    assert.match(viewer, /data-ground-phase=/)
    assert.match(viewer, /aria-hidden=/)
    assert.match(viewer, /inert=/)
  })
  it('leaving studio chrome takes no pointer', () => {
    const css = readFileSync(new URL('./site/landing.css', import.meta.url), 'utf8')
    const selector = '.studio-shell .app[data-studio-transition="out"] :is('
    const start = css.indexOf(selector)
    assert.ok(start >= 0)
    const open = css.indexOf('{', start)
    const close = css.indexOf('}', open)
    assert.ok(open > start && close > open)
    assert.match(css.slice(open + 1, close), /pointer-events:\s*none\s*;/)
  })
})

describe('W4g bleed-2a: one canvas across CAD and Solar CAD', () => {
  it('passes a palette revision to the studio viewer', () => {
    const viewer = appSource.slice(appSource.indexOf('const viewerEl = ('), appSource.indexOf('const legendEl ='))
    assert.match(viewer, /paletteRevision=\{studioGround \? \(surfaceSlots\.groundMaterial\.layerAccent === 'solar' \? 'solar' : 'base'\) : undefined\}/)
    assert.match(viewer, /colorForLayer=\{studioGround \? studioColorForLayer : surfaceColorForLayer\}/)
  })
  it('keeps the studio colour callback stable through its render-time ref', () => {
    assert.match(appSource, /surfaceColorForLayerRef\.current = surfaceColorForLayer/)
    assert.match(appSource, /const studioColorForLayer = useCallback\(\(layer\) => surfaceColorForLayerRef\.current\(layer\), \[\]\)/)
  })
  it('only rebuilds for callback identity when no revision is supplied', () => {
    assert.ok(!viewerSource.includes('[activeIntake, colorForLayer, background, panelSculpture]'))
    assert.ok(viewerSource.includes('paletteRevision === undefined'))
  })
  it('exports the in-place layer recolouring helper', () => {
    assert.match(viewerSource, /export function recolorLayerGroups\(/)
  })
  it('ignores zero-size resize notifications before resizing the renderer', () => {
    // The camera carry (W5 #123) resizes the renderer only when the mount size
    // changed; the zero-size return must still come first.
    assert.match(viewerSource, new RegExp('function onResize\\(\\)\\s*\\{\\s*const w = mount\\.clientWidth, h = mount\\.clientHeight\\s*if \\(!\\(w > 0\\) \\|\\| !\\(h > 0\\)\\) return\\s*if \\(w !== rendererWidth \\|\\| h !== rendererHeight\\) \\{\\s*rendererWidth = w; rendererHeight = h\\s*renderer\\.setSize\\('))
  })
})

const appNoComments = decomment(appSource)
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code
describe('studio unobstructed drawing viewport', () => {
  it('passes the measured safe rectangle only in the studio', () => {
    assert.match(appNoComments, new RegExp('safeRect=\\{studioGround \\? drawingViewport : null\\}'))
    assert.ok(appNoComments.includes('useDrawingViewport(studioGround && groundShowsDrawing(activeSurface) ? studioGround : null, STUDIO_DRAWING_OCCLUDERS)'))
    assert.ok(appNoComments.includes('drawingViewportRef.current = drawingViewport'))
  })
  it('uses safe bounds for result visibility and Show result framing', () => {
    const start = appNoComments.indexOf('const maxInvalidFrames =')
    const show = appNoComments.indexOf('const showCreatedResult =', start)
    const end = appNoComments.indexOf('const seatVersion =', show)
    const visibility = appNoComments.slice(start, show)
    const framing = appNoComments.slice(show, end)
    assert.ok(visibility.includes('drawingViewportRef.current'))
    assert.ok(visibility.includes('canvasRect.left + safe.left'))
    assert.ok(visibility.includes('< 8'))
    assert.ok(framing.includes('drawingViewportRef.current'))
    assert.ok(framing.includes('viewer.frame('))
    assert.ok(framing.includes('viewer.setView('))
  })
  it('names the existing and navigation occluders and excludes growing command chrome and the view cube', () => {
    assert.match(appNoComments, /import \{ STUDIO_DRAWING_OCCLUDERS \} from '.\/site\/drawingOccluders.js'/)
    const start = occluderSource.indexOf('const STUDIO_DRAWING_OCCLUDERS = Object.freeze(')
    assert.ok(start >= 0)
    const end = occluderSource.indexOf('\n])', start)
    assert.ok(end >= 0)
    const list = occluderSource.slice(start, end)
    for (const selector of ['header.top', '#drafting-ribbon', '.viewer-toolbar', '[data-testid="cockpit-view"]',
      '.properties-dock', '.bar.bar-command-line', 'footer.foot-bar', '.rail-stack']) assert.ok(list.includes(selector), selector)
    assert.ok(list.includes('reserve: 50'))
    for (const entry of ["['[data-nav-find]', 'top']", "['[data-cad-overview]', 'right']", "['[data-nav-objects]:not([open])', 'top']", "['[data-nav-objects][open]', 'left']"]) {
      assert.ok(list.includes(entry), entry)
    }
    assert.ok(!list.includes("'.bar-dock'"))
    assert.ok(!list.includes("['[data-cad-overview]', 'nearest']"))
    assert.ok(!list.includes("['[data-nav-objects]', 'nearest']"))
    assert.ok(!list.includes('cockpit-prompt'))
    assert.ok(!list.includes('cockpit-cube'))
  })
})
describe('Studio navigation shared wiring', () => {
  it('shares a live navigation source ref and the full hook return object', () => {
    assert.match(stripped, /const navigationSourceRef = useRef\(null\)/)
    const hook = stripped.slice(stripped.indexOf('useViewNavigation({'))
    assert.ok(hook.slice(0, hook.indexOf('});')).includes('navigationSource: navigationSourceRef'))
    assert.ok(stripped.includes('const viewNavigation = useViewNavigation({'))
    assert.match(stripped, /const\s*\{\s*pushView: pushViewSnapshot,\s*fit: fitWithHistory,\s*back: viewBack,\s*up: viewUp,\s*announcement: viewAnnouncement,?\s*\}\s*= viewNavigation/)
  })

  it('passes the shared viewer and navigation bindings to the navigation tools and editor', () => {
    const tools = appNoComments.match(/<DrawingNavigationTools\s[^>]*\/>/)?.[0]
    assert.ok(tools)
    for (const prop of ['viewerRef={viewerRef}', 'navigationSourceRef={navigationSourceRef}', 'navigation={viewNavigation}']) {
      assert.ok(tools.includes(prop), prop)
    }
    const editor = appNoComments.match(/<CadEditSurface\s[\s\S]*?\/>/)?.[0]
    assert.ok(editor?.includes('viewerRef={viewerRef}'))
  })

  it('mounts the overview directly after the tools only on the drawing ground', () => {
    assert.ok(appNoComments.includes("import CadOverview from './site/CadOverview.jsx'"))
    assert.match(appNoComments, new RegExp('<DrawingNavigationTools\\s[^>]*/>\\s*\\{studioGround && groundShowsDrawing\\(activeSurface\\) && <CadOverview viewerRef=\\{viewerRef\\} />\\}'))
    assert.equal((appNoComments.match(/<CadOverview\b/g) || []).length, 1)
    const overview = decomment(readFileSync(new URL('./site/CadOverview.jsx', import.meta.url), 'utf8'))
    assert.ok(overview.includes('export default function CadOverview('))
    assert.ok(overview.includes('data-cad-overview'))
  })
})
describe('S3 viewer Back and Up history wiring', () => {
  it('owns one bounded history plus a size counter, and the live hook survives comment stripping', () => {
    assert.equal((appNoComments.match(/createViewHistory\(\)/g) || []).length, 1)
    assert.ok(appNoComments.includes('const [viewHistorySize, setViewHistorySize] = useState(0)'))
    assert.ok(appNoComments.includes('history: viewHistoryRef.current, setHistorySize: setViewHistorySize'))
    assert.ok(stripped.includes('useViewNavigation({'))
  })
  it('pushes before Show result frames, inside the showCreatedResult slice', () => {
    const show = appNoComments.indexOf('const showCreatedResult =')
    const end = appNoComments.indexOf('const seatVersion =', show)
    assert.ok(show >= 0 && end > show)
    const framing = appNoComments.slice(show, end)
    const guard = framing.indexOf('if (!resultBounds) return')
    const push = framing.indexOf('pushViewSnapshot()')
    assert.ok(guard >= 0 && push > guard, 'the push follows the no-result guard')
    assert.ok(push < framing.indexOf('viewer.frame('), 'the push precedes the frame')
    assert.ok(push < framing.indexOf('viewer.setView('), 'the push precedes the setView fallback')
  })
  it('pushes before every other navigation jump: the ribbon Fit to bounds, the View cluster Fit and the cockpit Fit', () => {
    assert.match(appNoComments, /onClick=\{\(\) => \{ pushViewSnapshot\(\); viewerRef\.current\?\.fit\(\) \}\}>Fit to bounds</)
    assert.match(appNoComments, /const view = viewCluster\(\{[\s\S]*?onBeforeJump: pushViewSnapshot,[\s\S]*?\}\)/)
    assert.match(appNoComments, /<ViewCluster\s+viewerRef=\{viewerRef\}\s+onFit=\{fitWithHistory\}\s+canBack=\{viewHistorySize > 0\}\s+onBack=\{viewBack\}\s+onUp=\{viewUp\}/)
  })
  it('clears the history in resetDrawingSelection and in one effect keyed on the engine document id', () => {
    const start = appNoComments.indexOf('const resetDrawingSelection = useCallback(')
    const end = appNoComments.indexOf('const reportDrawingError', start)
    assert.ok(start >= 0 && end > start)
    assert.ok(appNoComments.slice(start, end).includes('clearViewHistory()'))
    const effects = appNoComments.match(/useEffect\(\(\) => \{ clearViewHistory\(\) \}, \[activeIntake\?\.documentId, clearViewHistory\]\)/g) || []
    assert.equal(effects.length, 1)
  })
})
describe('Start is a view inside the current workspace profile', () => {
  it('wires all three Start controls to one memory-only handler', () => {
    assert.match(appSource, new RegExp('className="doc-tab-start"\\s+onClick=\\{onOpenStart\\}'))
    assert.match(appSource, new RegExp('className="doc-tab-close"[\\s\\S]*?onClick=\\{onOpenStart\\}'))
    assert.match(appSource, new RegExp('<StatusTabs[^>]+onStart=\\{onOpenStart\\}'))
    assert.doesNotMatch(appNoComments, /onSelectSurface\('browser'\)/)
    const start = appNoComments.indexOf('const onOpenStart =')
    const end = appNoComments.indexOf('const returnToDrawing =', start)
    const handler = appNoComments.slice(start, end)
    assert.match(handler, /if\s*\(startOpenRef\.current\)\s*return\s+startOpenerRef\.current\s*=/)
    assert.match(handler, /setStartOpen\(true\)/)
    assert.doesNotMatch(handler, /onSelectSurface|setActiveSurface|history\.|dispatchEvent|setOpenProject/)
    assert.match(appNoComments, /\[startOpen, setStartOpen\] = useState\(false\)/)
  })

  it('requests heading focus from the board only when Start opens', () => {
    assert.match(appNoComments, /\[startFocusRequest, setStartFocusRequest\] = useState\(0\)/)
    const start = appNoComments.indexOf('const onOpenStart =')
    const end = appNoComments.indexOf('const returnToDrawing =', start)
    assert.match(appNoComments.slice(start, end), /setStartFocusRequest\(\(request\) => request \+ 1\)/)
    assert.equal((appNoComments.match(/setStartFocusRequest\(/g) || []).length, 1)
    assert.match(appNoComments, /<SurfaceGrounds\s[\s\S]*?startFocusRequest=\{startFocusRequest\}/)
    assert.doesNotMatch(appNoComments, /boardHeadingRef\.current\?\.focus\(\)/)
  })

  it('returns to the drawing at engine, catalog and version sinks', () => {
    assert.match(appNoComments, new RegExp('<EngineSessionProvider[^>]+onBeforeEdit=\\{closeStartForChange\\}'))
    const runStart = appNoComments.indexOf('const onRun = useCallback')
    const runEnd = appNoComments.indexOf('const onConfirmCatalogRun =', runStart)
    assert.notEqual(runStart, -1)
    assert.notEqual(runEnd, -1)
    assert.match(appNoComments.slice(runStart, runEnd), /if\s*\(writeLocked && isWrite\)\s*return null\s+closeStartForChange\(\)/)
    for (const [name, target] of [
      ['onPreviewVersionTracked', 'onPreviewVersion'],
      ['onBackToHeadTracked', 'onBackToHead'],
      ['onRestoredTracked', 'onRestoreCommitted'],
    ]) {
      const start = appNoComments.indexOf(`const ${name} = useCallback`)
      const end = appNoComments.indexOf('])', start)
      assert.notEqual(start, -1)
      assert.notEqual(end, -1)
      const callback = appNoComments.slice(start, end + 2)
      assert.match(callback, new RegExp('closeStartForChange\\(\\)[\\s\\S]*?return ' + target + '\\(\\.\\.\\.args\\)'))
      assert.match(callback, new RegExp('\\[' + target + ', closeStartForChange\\]'))
    }
    for (const [prop, callback] of [
      ['onPreview', 'onPreviewVersionTracked'],
      ['onBackToHead', 'onBackToHeadTracked'],
      ['onRestored', 'onRestoredTracked'],
    ]) {
      assert.match(appNoComments, new RegExp('<VersionHistory\\s[^>]*' + prop + '=\\{' + callback + '\\}'))
    }
  })

  it('closes Start for changes without a synchronous flush or focus move', () => {
    const start = appNoComments.indexOf('const closeStartForChange = useCallback')
    const end = appNoComments.indexOf('const onReturnToDrawing =', start)
    assert.notEqual(start, -1)
    assert.notEqual(end, -1)
    const close = appNoComments.slice(start, end)
    assert.match(close, /if\s*\(!startOpenRef\.current\)\s*return/)
    assert.match(close, /startOpenRef\.current\s*=\s*false/)
    assert.match(close, /setStartOpen\(false\)/)
    assert.doesNotMatch(close, /flushSync|focus/)
    for (const [component, prop] of [
      ['EngineSessionProvider', 'onBeforeEdit'],
      ['ConversePanel', 'onBeforeWriteApproval'],
      ['VersionHistory', 'onBeforeRestore'],
    ]) {
      const mountStart = appNoComments.indexOf('<' + component)
      assert.notEqual(mountStart, -1)
      const mountEnd = appNoComments.indexOf('/>', mountStart)
      assert.notEqual(mountEnd, -1)
      assert.ok(appNoComments.slice(mountStart, mountEnd).includes(prop + '={closeStartForChange}'))
    }
  })

  it('closes Start before either viewer recovery request', () => {
    assert.match(appNoComments, /retryRefresh:\s*onRetryViewerRefreshRaw/)
    assert.match(appNoComments, /retryUnreadableHead:\s*retryUnreadableHeadRaw/)
    for (const name of ['onRetryViewerRefresh', 'retryUnreadableHead']) {
      const start = appNoComments.indexOf('const ' + name + ' = useCallback')
      const end = appNoComments.indexOf('])', start)
      assert.notEqual(start, -1)
      assert.notEqual(end, -1)
      const callback = appNoComments.slice(start, end + 2)
      assert.match(callback, new RegExp('closeStartForChange\\(\\)\\s+return '
        + name + 'Raw\\(\\.\\.\\.args\\)'))
      assert.match(callback, new RegExp('\\[' + name + 'Raw, closeStartForChange\\]'))
    }
  })
  it('shows the Run and Build gloss beside the shared prompt for every visible board', () => {
    const dock = appNoComments.indexOf('<div className="bar-dock">')
    assert.notEqual(dock, -1)
    const gloss = appNoComments.slice(dock, dock + 280)
    assert.match(gloss, /boardVisible &&/)
    assert.match(gloss, /START_BOARD_COPY\.gloss/)
    assert.match(gloss, /mock &&/)
    assert.doesNotMatch(gloss, /openProjectId/)
    assert.match(appNoComments, new RegExp('const shell = \\{\\s*startOpen,'))
    assert.match(appNoComments, /onCloseStart: onReturnToDrawing/)
    assert.match(appNoComments, new RegExp('boardVisible=\\{boardVisible\\}'))
  })
})
const promptBoxSessionBinding = /React\.createElement\(\s*PromptBox,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/
const conversePanelSessionBinding = /React\.createElement\(\s*ConversePanel,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/

// ---------------------------------------------------------------------------
// Orphaned-setter scan (slice 4a). Every `setX(...)` a module CALLS must have a
// declaration somewhere in that module: a useState destructure, a controller
// destructure (`const { setOverlayStale } = drawing`, including renames like
// `reportError: setRunErr`), a plain binding, a parameter, or a browser global.
//
// esbuild's output keeps ordinary comments, so a `setX(` written inside one
// would otherwise read as a live call: strip comments before scanning, or the
// pin reports a phantom orphan and gets muted by the next person.
//
// A naive `line.indexOf('//')` has the opposite failure: a string literal
// containing a URL (`'https://example.com'`) puts a `//` on the line before
// any real comment does, so the cut lands inside the string and silently
// drops every token after it — including a genuine setter call later on that
// same line.
//
// The two failures cannot be fixed as two independent regex passes run in
// either order: masking string literals BEFORE stripping comments treats
// every English contraction inside a comment ("the controller's alone",
// "this render's decision") as an unterminated string that swallows
// thousands of real characters — including live useState declarations —
// until the next stray apostrophe; stripping comments first, with a
// naive scanner, is exactly the original `//`-in-a-URL bug. Only a single
// pass that tracks "am I inside a string right now" and "am I inside a
// comment right now" as ONE mutually-exclusive state can get both right:
// a `//` is a comment only when no string is open, and a quote opens a
// string only when no comment is open (so an apostrophe on a commented-out
// line, or a `//` inside a real string, both stay inert).
// ---------------------------------------------------------------------------
const SETTER = 'set[A-Z][\\w$]*'
const BROWSER_SETTERS = new Set(['setTimeout', 'setInterval'])

function decomment(code) {
  let out = ''
  let i = 0
  const n = code.length
  while (i < n) {
    const ch = code[i]
    const next = code[i + 1]
    if (ch === '/' && next === '*') {
      i += 2
      while (i < n && !(code[i] === '*' && code[i + 1] === '/')) i++
      i += 2
      out += ' '
      continue
    }
    if (ch === '/' && next === '/') {
      i += 2
      while (i < n && code[i] !== '\n') i++
      continue // the newline itself is copied on the next loop iteration
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      const quote = ch
      out += ch
      i++
      while (i < n && code[i] !== quote) {
        if (code[i] === '\\' && i + 1 < n) {
          out += code[i] + code[i + 1]
          i += 2
          continue
        }
        out += code[i]
        i++
      }
      if (i < n) { out += code[i]; i++ } // the closing quote
      continue
    }
    out += ch
    i++
  }
  return out
}

function orphanSetters(jsxSource) {
  const code = decomment(esbuild.transformSync(jsxSource, { loader: 'jsx' }).code)
  const declared = new Set(BROWSER_SETTERS)
  let match
  // Lookahead, never consume, on the trailing delimiter: two setters adjacent
  // in one destructure share the comma between them, and a consuming match
  // would swallow it and hide the second one.
  for (const pattern of [
    new RegExp(`\\[[^\\]]*?(${SETTER})\\s*(?=[,\\]])`, 'g'),
    new RegExp(`(?:[{,:]\\s*)(${SETTER})\\s*(?=[,}=])`, 'g'),
    new RegExp(`(?:const|let|var|function)\\s+(${SETTER})`, 'g'),
    new RegExp(`[(,]\\s*(${SETTER})\\s*(?=[,)=])`, 'g'),
  ]) while ((match = pattern.exec(code))) declared.add(match[1])

  const called = new Set()
  const callPattern = new RegExp(`(?<![.\\w$])(${SETTER})\\s*\\(`, 'g')
  while ((match = callPattern.exec(code))) called.add(match[1])
  return [...called].filter((name) => !declared.has(name)).sort()
}

// Bindings App declares AND passes into JSX. Add a row whenever a new one is
// introduced; the cost is one line and the failure it catches is a white screen.
const DECLARED_AND_USED = [
  { name: 'slashCommandActions', usedAs: 'commandActions' },
  { name: 'registryEntries', usedAs: 'registryEntries' },
  { name: 'catalogSkills', usedAs: 'skills: catalogSkills' },
  { name: 'agentSessionId', usedAs: 'sessionId: agentSessionId' },
]

describe('App.jsx wiring', () => {
  it('names the drawing refresh wait when interrupting a finished job', () => {
    const start = stripped.indexOf('onInterruptRun: () => {')
    const end = stripped.indexOf('onClearSelection:', start)
    assert.ok(start >= 0 && end > start, 'interrupt handler must survive the transform')
    const handler = stripped.slice(start, end)
    assert.match(handler, new RegExp('showToast\\(result != null\\s*\\?\\s*\\{\\s*text:\\s*`Stopped waiting for the drawing to refresh after \\$\\{tool\\}\\.`'))
    assert.match(stripped, /interruptRun,\s*currentJob\?\.tool,\s*currentJob\?\.job_id,\s*result,\s*mock,\s*showToast/)
  })

  it('acknowledges interrupting live and mock runs after detaching', () => {
    const start = stripped.indexOf('onInterruptRun: () => {')
    const end = stripped.indexOf('onClearSelection:', start)
    assert.ok(start >= 0 && end > start, 'interrupt handler must survive the transform')
    const handler = stripped.slice(start, end)
    assert.match(handler, /interruptRun\(\);[\s\S]*showToast\(/)
    assert.match(handler, /typeof currentJob\?\.tool === "string" && currentJob\.tool\.trim\(\) \? currentJob\.tool : "the run"/)
    assert.match(handler, new RegExp('text:\\s*mock\\s*\\?\\s*`Stopped waiting for \\$\\{tool\\}\\.`\\s*:\\s*currentJob\\?\\.job_id\\s*\\?\\s*`Stopped following \\$\\{tool\\}\\. It keeps running; find it in Jobs\\.`\\s*:\\s*`Stopped waiting for \\$\\{tool\\} to be accepted\\. If the server accepts it, it will appear in Jobs\\.`'))
  })

  it('names the quick tools as version history actions', () => {
    assert.match(stripped, /id: "quick-undo",\s*label: "Undo version"/)
    assert.match(stripped, /id: "quick-redo",\s*label: "Redo version"/)
  })

  it('only swallows parseable unarmed points, so comma prose reaches the catalog', () => {
    assert.match(stripped, /if \(parsePointExpression\(text\) !== null\)/)
    assert.doesNotMatch(stripped, /isPointExpression\(text\)/)
  })
  describe('S03 one drawing truth', () => {
    // The engine paints the viewer while shown; the console intake remains
    // the exact fallback when the consumer closes or unmounts.
    it('binds selection, geometry, legend, dock totals and status to the active intake', () => {
      assert.match(stripped, /const drawingIntake = activeIntake \|\| shown/)
      assert.match(stripped, /countEntitiesByLayer\(drawingIntake\)/)
      assert.match(stripped, /selectEntity\(drawingIntake, selectedHandle,/)
      assert.match(stripped, /drawingIntake\.polylines \|\| \[\]/)
      assert.match(stripped, /drawingExtents\(drawingIntake\.polylines\)/)
      assert.match(appNoComments, new RegExp('<Legend\\s+layers=\\{drawingIntake.layers\\}'))
      assert.match(appNoComments, new RegExp('<CockpitStatus[^>]*shown=\\{drawingIntake\\}'))
      assert.match(appNoComments, new RegExp('onShown=\\{\\(intake, history\\) => \\{\\s*setActiveIntake\\(intake\\)'))
      assert.match(appNoComments, new RegExp('onHidden=\\{\\(\\) => \\{\\s*setActiveIntake\\(null\\)'))
      assert.match(appNoComments, /undoDepth: history.undoDepth, redoDepth: history.redoDepth/)
    })
    it('drops a result from a different document before resolving its handle', () => {
      assert.match(appNoComments, new RegExp('if \\(!resultCandidate \\|\\| !activeIntake \\|\\| resultCandidate.documentId !== activeIntake.documentId\\) return null'))
      assert.match(appNoComments, new RegExp('if \\(!intake \\|\\| \\(resultCandidate && resultCandidate.documentId !== intake.documentId\\)\\) \\{\\s*setResultCandidate\\(null\\)\\s*setOffscreenResult\\(null\\)'))
      assert.match(appNoComments, new RegExp('if \\(intake && history\\?\\.createdResult\\?\\.documentId === intake.documentId\\)'))
    })
    it('passes the visibility result and viewer pose handler to the dock', () => {
      assert.match(appNoComments, new RegExp('offscreenResult=\\{offscreenResult\\}\\s+onShowResult=\\{showCreatedResult\\}'))
      assert.match(stripped, new RegExp('viewer\\.setView\\(\\{\\s*center:'))
      assert.match(stripped, /pose\.zoom \* 0\.4 \/ span/)
    })
  })

  describe('W4g selection mirror back', () => {
    it('mounts StatusModesBridge under the engine flag', () => {
      assert.match(appSource, /\{ENV_CAD_EDIT && drafting && <StatusModesBridge \/>\}/)
    })
    it('passes the console selection setter to EngineDocumentView', () => {
      assert.match(appSource, /<EngineDocumentView\b(?:(?!\/>)[\s\S])*?\bonSelectedHandleChange=\{setSelectedHandle\}/)
    })
  })

  for (const { name, usedAs } of DECLARED_AND_USED) {
    it(`declares ${name} in executable code, not inside a comment`, () => {
      // Matches a plain binding (`const x =`) and an array destructure
      // (`const [x, setX] =`), which is how useState results are bound.
      const declared = new RegExp(
        `(const|let|var)\\s+(\\[\\s*)?${name}\\s*[,\\]=]`).test(stripped)
        || new RegExp(
          `(const|let|var)\\s+\\{[^}]*\\b(?:\\w+\\s*:\\s*)?${name}\\s*[,}]`).test(stripped)
      assert.ok(declared,
        `${name} does not survive comment-stripping — its declaration is inside a ` +
        'comment block, so any render that reads it throws ReferenceError')
    })

    it(`actually uses ${name} (${usedAs}) after declaring it`, () => {
      assert.ok(stripped.includes(usedAs),
        `${usedAs} is not referenced in the compiled output — the wiring is dead`)
    })
  }

  // Slice 3: "run the tool I just authored" is ONE honest path on both shells.
  // App used to arm the run straight off the provisional publish response, so a
  // publish that had not settled in the runnable catalog armed anyway. These
  // pins are source-level for the same reason the ones above are: nothing here
  // is reachable without rendering App, and publishedCatalogTool.test.mjs
  // remains the single oracle for the resolver's own behaviour.
  describe('authored-tool run path', () => {
    const useAuthoredBody = () => {
      const start = stripped.indexOf('onUseAuthored = useCallback')
      assert.notEqual(start, -1, 'onUseAuthored is not declared in executable code')
      return stripped.slice(start, start + 1400)
    }

    it('refetches the runnable catalog before it resolves anything', () => {
      assert.match(useAuthoredBody(), /await\s+loadCatalogTools\(\)/)
    })

    it('resolves through publishedCatalogTool, the one oracle', () => {
      assert.ok(stripped.includes('resolvePublishedCatalogTool'),
        'App does not import/call the shared published-tool resolver')
      assert.match(useAuthoredBody(), /resolvePublishedCatalogTool\(/)
    })

    it('surfaces the resolver message instead of arming the run', () => {
      const body = useAuthoredBody()
      assert.match(body, /catch/)
      assert.match(body, /showToast\(/)
      // The bail is BEFORE the commit, or the honesty is decorative.
      assert.ok(body.indexOf('showToast(') < body.indexOf('commitCatalogDecision('),
        'the error toast must precede the commit, not follow it')
    })

    it('stamps the authored provenance ToolCast already stamps', () => {
      assert.match(useAuthoredBody(), /source:\s*"authored"/)
    })

    it('hands the refetched record to armDecision instead of the stale list', () => {
      assert.match(useAuthoredBody(), /refreshedTool:\s*runnableTool/)
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      const armBody = stripped.slice(armStart, armStart + 1200)
      assert.match(armBody, /decision\.refreshedTool\?\.name === decision\.tool/)
    })

    it('fails when the resolver call is removed', () => {
      const mutated = appSource.replace(
        /runnableTool = resolvePublishedCatalogTool\(tool, refreshedTools\)/,
        'runnableTool = tool',
      )
      assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
      const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
      const start = mutatedStripped.indexOf('onUseAuthored = useCallback')
      assert.doesNotMatch(mutatedStripped.slice(start, start + 1400),
        /resolvePublishedCatalogTool\(/)
    })
  })

  it('passes sessionId into the PromptBox element itself', () => {
    // Limit the match to the PromptBox props object, not another component
    // receiving the same session binding.
    assert.match(stripped, promptBoxSessionBinding)
  })

  it('rejects PromptBox sessionId={null} even while ConversePanel keeps its binding', () => {
    const mutated = appSource.replace(
      /(<PromptBox[\s\S]*?\bsessionId=\{)agentSessionId(\})/,
      '$1null$2',
    )
    assert.notEqual(mutated, appSource, 'the falsification mutation must target PromptBox')
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    assert.match(mutatedStripped, conversePanelSessionBinding)
    assert.doesNotMatch(mutatedStripped, promptBoxSessionBinding)
  })

  it('disables image paste in the catalog command bar instead of dropping an attachment', () => {
    assert.match(stripped, /imageAttachmentsEnabled:\s*false/)
    const promptBox = readFileSync(new URL('./components/PromptBox.jsx', import.meta.url), 'utf8')
    assert.match(promptBox, /if \(!imageAttachmentsEnabled\)/)
    assert.match(promptBox, /Image paste is available in the assistant reply box/)
  })

  it('seats live intake with the mapped store drawing and its durable version summary', () => {
    // Bind the load to its starting identity so a later identity cannot receive its result.
    assert.match(appSource, /\/\/ load session \(intake[^\n]*\n\s*useEffect[^\n]*\n\s*let alive = true\s*const loadDrawingId = REQUESTED_DRAWING_ID/)
    assert.match(
      stripped,
      /drawingSummary\s*=\s*await getDrawingVersions\(false,\s*loadDrawingId\)/,
    )
    assert.match(
      stripped,
      /drawingId:\s*loadDrawingId[\s\S]*drawingState:\s*drawingSummary/,
    )
    assert.match(stripped, /fallbackDrawingId:\s*REQUESTED_DRAWING_ID/)
  })

  // Standardization slice 4a wrote this after making the exact mistake it
  // catches. Lifting the nav rail into site/NavRail.jsx moved the author fold's
  // `const [authorOpen, setAuthorOpen] = useState(false)` out of App while
  // SEVEN build-lane call sites kept calling setAuthorOpen(true). `npm run
  // build` passed (an undefined identifier in a callback is a RUNTIME
  // ReferenceError, not a build error), every unit row passed, and the first
  // click that opened the author panel would have thrown.
  //
  // The rows above catch a declaration swallowed by a comment; this catches a
  // declaration DELETED while its callers stayed, which is the shape every
  // extract-a-component refactor can produce. Generic on purpose: it needs no
  // maintenance when a new piece of state arrives.
  //
  // Slice 4a split the console's shell across four files (App.jsx plus the
  // extracted site/ToolCast.jsx, site/SurfaceFrame.jsx and site/NavRail.jsx),
  // and the exact mistake this pin exists for — a useState hoisted out while
  // its call sites stayed behind — can land in any one of the four just as
  // easily as it landed in App. Loop the same scan over all four; each module
  // is scanned on its own, since a setter genuinely declared in one and
  // called from another (a prop, not a closure) is not an orphan and this pin
  // must not treat cross-module wiring as a defect.
  const ORPHAN_SETTER_SOURCES = [
    { label: 'App.jsx', path: './App.jsx' },
    { label: 'site/ToolCast.jsx', path: './site/ToolCast.jsx' },
    { label: 'site/SurfaceFrame.jsx', path: './site/SurfaceFrame.jsx' },
    { label: 'site/NavRail.jsx', path: './site/NavRail.jsx' },
  ]

  for (const { label, path } of ORPHAN_SETTER_SOURCES) {
    it(`${label} calls no useState setter it does not declare (orphaned setter after a hoist)`, () => {
      const source = readFileSync(new URL(path, import.meta.url), 'utf8')
      assert.deepEqual(orphanSetters(source), [],
        `${label} calls these setters but declares none of them: a component `
        + 'extraction took the useState with it and left the call sites behind')
    })
  }

  it('fails when a setter declaration is deleted from under its callers', () => {
    // Falsification: remove the state setter used by the author-panel wrapper.
    const mutated = appSource.replace(
      /const \[authorOpen, setAuthorOpenState\] = useState\(false\)/, '')
    assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
    assert.deepEqual(orphanSetters(mutated), ['setAuthorOpenState'])
  })

  it('refuses a live legacy write when version bootstrap did not produce a pin', () => {
    assert.match(
      stripped,
      /!mock\s*&&\s*isWrite\s*&&\s*catalogRunContextRef\.current\.projectId\s*==\s*null\s*&&\s*catalogRunContextRef\.current\.drawingVersion\s*==\s*null[\s\S]*setRunErr\([\s\S]*return/,
    )
  })

  it('derives the submitted drawing version from the tool capability', () => {
    assert.match(
      stripped,
      /dwgVersion:\s*drawingVersionForRun\(tool,\s*executionContext,\s*health\?\.aps_live\)/,
    )
  })

  it('binds the status bar regions to the width whose CSS styles them', () => {
    // P1 studio-shell pass. The three FootRegion wrappers are a WIDE-layout
    // construct: cockpit.css styles `.foot-region` only inside
    // `@media (min-width: 981px)`. Below that the old shell's own rules do
    // the work, and they are CHILD selectors — `footer.foot-bar > *`
    // (styles.css:776-777) — so a wrapper there would hide every segment
    // from the only rules that style it, and the narrow status bar would
    // lose its layout silently on a viewport no unit test renders at.
    //
    // The gate is therefore `wideViewport`, which is literally
    // matchMedia('(min-width: 981px)') in this file: the same breakpoint,
    // read once. Pinned in the transform (comments stripped) so the term
    // cannot be dropped as "redundant" by a later reader.
    assert.match(
      stripped,
      /footRegions\s*=\s*studioShell\s*&&\s*wideViewport/,
    )
    // ...and that gate is the ONLY thing deciding it, on every FootRegion:
    // three mounts, each reading the same binding, so they cannot disagree
    // about whether the bar is regioned and leave a half-wrapped footer.
    // Built from a string, not a regex literal: the honesty gate masks
    // comments and STRINGS and then balances braces per file, and it cannot
    // see a regex literal, so an escaped `\{` with no partner in one reads to
    // it as an unclosed block and the whole file is reported as untrustworthy
    // ("braces do not balance after comment/string masking"). Same pattern,
    // same match; the braces now sit inside a masked string.
    const gated = stripped.match(new RegExp('React\\.createElement\\(\\s*FootRegion,\\s*\\{\\s*on:\\s*footRegions\\b', 'g')) || []
    assert.equal(gated.length, 3, `expected 3 FootRegion mounts on footRegions, saw ${gated.length}`)
    assert.equal((stripped.match(/React\.createElement\(\s*FootRegion\b/g) || []).length, 3)
  })

  it('keeps the wide drafting properties dock mounted before drawing data exists', () => {
    // Standardization slice 2 renamed the surface term only: the mount gate is
    // now `dockSections` (the contract's rails.dock, truthy exactly where
    // `drafting` was) instead of `drafting`. What this pin guards is unchanged:
    // the dock must mount on a wide drafting surface BEFORE any drawing data
    // exists, so the entitlement controls cannot vanish into an honest-empty
    // state. The negative below is the regression it was written for: gating
    // the mount on data (legendEl || readoutEl) is the white-screen shape.
    assert.match(
      stripped,
      // A string for the same reason as the FootRegion pin above.
      new RegExp('if \\(studioGround && dockSections && wideViewport\\) \\{\\s*return[^;]*React\\.createElement\\(\\s*PropertiesDock'),
    )
    assert.doesNotMatch(
      stripped,
      /studioGround && (?:dockSections|drafting) && wideViewport && \(legendEl \|\| readoutEl\)/,
    )
  })

  // W4g-3b (one head): a save that carries a plan (the store's diff of the
  // head document) goes to the plan route, where the SERVER picks the commit
  // leg; a hand import (no plan) keeps the F-3 sidecar route. The opener is
  // the one caller that marks its load as the head.
  describe('W4g-3b: a save with a plan takes the plan route', () => {
    it('routes by the plan, inside the same checkout the F-3 save takes', () => {
      const start = stripped.indexOf('engineSaveTarget = ')
      assert.notEqual(start, -1)
      const body = stripped.slice(start, start + 4400)
      assert.match(body, /save: async \(bytes, parent, digest, plan = null, onStatus = null\)/)
      // W1: a hand import gets the real parent, and an unknown job keeps its fence.
      assert.match(body, /if \(parent == null\)\s*parent = \(await getDrawingVersions\(false, REQUESTED_DRAWING_ID\)\)\.head/)
      assert.match(body, /keepLock = error\?\.outcomeUnknown === true/)
      assert.match(body, /if \(acquired && !keepLock\)/)
      // W2: an unknown save reuses its own checkout until a terminal outcome.
      assert.match(stripped, /pendingSavesRef = useRef\((?:\/\*[^*]*\*\/\s*)?new Map\(\)\)/)
      assert.match(body, /pendingSavesRef\.current\.get\(REQUESTED_DRAWING_ID\)/)
      assert.match(body, /pendingSavesRef\.current\.set\(REQUESTED_DRAWING_ID/)
      assert.match(body, /pendingSavesRef\.current\.delete\(REQUESTED_DRAWING_ID\)/)
      const planCall = body.indexOf('saveDrawingVersionPlan(')
      const sidecarCall = body.indexOf('saveEditedDrawingVersion(')
      assert.notEqual(planCall, -1)
      assert.notEqual(sidecarCall, -1)
      assert.doesNotMatch(body, /save(?:DrawingVersionPlan|EditedDrawingVersion)\([^)]*chain\.head/)
      assert.match(body, /saveDrawingVersionPlan\(\s*REQUESTED_DRAWING_ID,\s*bytes,\s*parent,\s*digest,\s*plan\.mutations,\s*cap,\s*\{\s*onStatus\s*\}\s*\)/)
      assert.match(body, /saveEditedDrawingVersion\(\s*REQUESTED_DRAWING_ID,\s*bytes,\s*parent,\s*digest,\s*cap\s*\)/)
      assert.ok(planCall < sidecarCall, 'the plan route is tried first, behind the plan check')
      assert.match(body.slice(0, planCall), /if \(plan && plan\.mutations\)/)
      // Both calls sit inside the acquire -> save -> release try block.
      assert.ok(body.indexOf('try {') < planCall && body.indexOf('} finally {') > sidecarCall)
    })

    it('the opener marks its load as the head, so the store keeps a diff base', () => {
      const opener = readFileSync(new URL('./cadedit/EngineHeadOpener.jsx', import.meta.url), 'utf8')
      assert.match(opener, /openBytes\(bytes, headDocumentId\(drawingId, version\), \{ committed: true, version \}\)/)
    })
  })

  // W4g-2 (one head), kimi on #1008 finding 1: the dirty refusal must hold at
  // EXECUTION time, not only when the run is armed. The confirm strip stays
  // open while the drafter keeps drawing (engine tools never touch the run
  // intent), so onRun, every path's last stop before runJob, reads the
  // CURRENT fact from a ref and refuses before anything is submitted.
  describe('W4g-2 one head: unsaved browser edits refuse a write tool at execution time', () => {
    const onRunStart = stripped.indexOf('onRun = useCallback')
    const onRunBody = stripped.slice(onRunStart, onRunStart + 2600)

    it('onRun refuses a write tool while the engine is dirty, before runJob', () => {
      assert.notEqual(onRunStart, -1)
      const refusal = onRunBody.indexOf('engineDirtyRef.current')
      const run = onRunBody.indexOf('runJob(')
      assert.notEqual(refusal, -1, 'onRun must read engineDirtyRef.current')
      assert.notEqual(run, -1)
      assert.ok(refusal < run, 'the dirty refusal must precede runJob')
      assert.match(onRunBody.slice(refusal, run), /REASONS\.unsavedEngineEdits[\s\S]*return null/)
    })

    it('the ref and the ribbon state are written by the one dirty-change callback the provider gets', () => {
      // Read as text spans rather than one brace-carrying pattern: the
      // honesty gate's scanner masks comments and strings and then balances
      // braces per file, and an escaped `\{` inside a regex literal reads to
      // it as an unclosed block (it reported this very file rather than
      // silently trusting it).
      const start = stripped.indexOf('onEngineDirtyChange = useCallback')
      assert.notEqual(start, -1, 'App must declare onEngineDirtyChange')
      const body = stripped.slice(start, start + 220)
      assert.match(body, /engineDirtyRef\.current = !!dirty/)
      assert.match(body, /setEngineDirty\(!!dirty\)/)
      const provider = stripped.indexOf('React.createElement(EngineSessionProvider')
      const providerLoose = provider === -1 ? stripped.indexOf('EngineSessionProvider,') : provider
      assert.notEqual(providerLoose, -1, 'the provider must be mounted')
      assert.match(stripped.slice(providerLoose, providerLoose + 400), /onDirtyChange:\s*onEngineDirtyChange/)
    })

    it('armDecision and onRun share the one write predicate', () => {
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      assert.match(stripped.slice(armStart, armStart + 1600), /isWrite = isWriteTool\(catalogTool\)/)
      assert.match(onRunBody, /isWrite = isWriteTool\(tool\)/)
    })

    it('arms after the platform session resolves even when the auth-only tenant echo is absent', () => {
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      const armBody = stripped.slice(armStart, armStart + 5200)
      assert.ok(armBody.includes('if (!mock && session.status !== "active") return'))
      assert.equal(armBody.includes('if (!mock && !tenant) return'), false)
      const dependencyStart = armBody.indexOf('[tools, mock, session.status')
      assert.notEqual(dependencyStart, -1)
    })

    it('the ribbon memo recomputes when the engine dirty state flips', () => {
      // The deps array that closes the ribbon useMemo names engineDirty
      // (kimi on #1008 finding 2: read inside, absent from the deps).
      assert.match(stripped, /lastAuthoredTool,\s*onUseAuthored,\s*setFamilyOpen,\s*engineDirty\s*\]\)/)
    })

    it('fails when the execution-time refusal is removed', () => {
      const mutated = appSource.replace(/if \(isWrite && engineDirtyRef\.current\) \{[\s\S]*?return null\r?\n\s*\}\r?\n/, '')
      assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
      const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
      const start = mutatedStripped.indexOf('onRun = useCallback')
      assert.doesNotMatch(mutatedStripped.slice(start, start + 2600), /engineDirtyRef\.current/)
    })
  })

  // W4g-1c (engine reach on the public demo): the opener mounts for mock
  // too, with the static sample DXF as its head at version 1; live keeps the
  // DXF route and the server's head. The rail-OFF and non-drafting gates are
  // unchanged (studioGround, drafting, intake).
  it('the head opener reaches the public demo through the static sample DXF', () => {
    assert.match(
      stripped,
      /React\.createElement\(\s*EngineHeadOpener,\s*\{[^}]*enabled:\s*!!studioGround && !!drafting && !!intake/,
    )
    assert.match(stripped, /headKey:\s*drawingState\?\.head \?\? \(mock \? 1 : null\)/)
    assert.match(stripped, /fetchDxf:\s*mock \? fetchSampleDxf : fetchDrawingDxf/)
    assert.match(stripped, /sourceKey:\s*mock \? "sample" : "live"/)
    assert.doesNotMatch(stripped, /enabled:\s*!!studioGround && !!drafting && !mock/)
  })

  it('passes the browser engine dirty state to the assistant approval card', () => {
    const binding = new RegExp('(<ConversePanel\\b(?:(?!/>)[\\s\\S])*?)\\s+engineDirty=\\{engineDirty\\}')
    assert.match(appSource, binding)
    const mutated = appSource.replace(binding, '$1')
    assert.notEqual(mutated, appSource, 'the falsification mutation must remove the ConversePanel attribute')
    assert.doesNotMatch(mutated, binding)
  })

  it('limits studio point picking and cursor readout to the viewer canvas', () => {
    for (const component of ['CanvasPointPicker', 'CockpitStatus']) {
      const mount = appNoComments.match(new RegExp('<' + component + '\\s[\\s\\S]*?/>'))?.[0]
      assert.ok(mount, component + ' mount exists')
      assert.match(mount, /canvasSelector="\.viewer-canvas"/)
      assert.match(mount, new RegExp('ground=\\{studioGround\\}'))
    }
  })

  it('opts studio frame and grounds into customer copy with explicit mock state', () => {
    const grounds = appNoComments.match(/<SurfaceGrounds\s[\s\S]*?\/>/)?.[0]
    assert.ok(grounds, 'SurfaceGrounds mount exists')
    assert.match(grounds, new RegExp('occluders=\\{STUDIO_DRAWING_OCCLUDERS\\}'))
    assert.match(grounds, /onCreateProject=\{onCreateProject\}/)
    for (const component of ['SurfaceFrame', 'SurfaceGrounds']) {
      const mount = new RegExp('<' + component + '\\s[\\s\\S]*?/>|<' + component + '\\s[\\s\\S]*?>')
      // J1 gives the ground nested JSX panel props; its first /> now closes
      // ProjectStartPanel, not SurfaceGrounds. Read through the portal argument.
      const source = component === 'SurfaceGrounds'
        ? appNoComments.match(/<SurfaceGrounds\s[\s\S]*?\/>,/)?.[0]
        : appNoComments.match(mount)?.[0]
      assert.ok(source, component + ' mount exists')
      assert.match(source, new RegExp('studioPresentation=\\{Boolean\\(studioGround\\)\\}'))
      assert.match(source, new RegExp('mock=\\{mock\\}'))
    }
  })
})

// sf-w3-conversion-graph-surface. Text pins over App.jsx (they pin the wiring, not the behaviour; the
// behaviour rows live in SolarToolForm.test.jsx CS/TF and ribbonClusters.test.js CS).
describe('Conversion surface wiring', () => {
  it('CW3 prepareRunParams admits selection overlays through the tool params schema', () => {
    const start = appSource.indexOf('const prepareRunParams = useCallback')
    const end = appSource.indexOf('}, [selectedHandle])', start)
    assert.ok(start >= 0 && end > start)
    const prepare = appSource.slice(start, end)
    assert.ok(prepare.includes('const overlays = admittedOverlays(tool?.params, catalogRunOverlays({'))
    assert.ok(prepare.indexOf('admittedOverlays(') < prepare.indexOf('catalogRunOverlays({'))
  })

  it('CW1 the generic Solar form reads the drawing intake for its graph revision', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const start = live.search(/React\.createElement\(\s*SolarToolForm,/)
    assert.ok(start >= 0)
    const form = live.slice(start, start + 700)
    assert.match(form, /readIntake: SOLAR_SETTINGS_LOADERS\?\.readIntake/)
    assert.match(form, /drawingId: catalogRunContext\?\.drawingId \?\? null/)
    assert.match(form, /drawingVersion: catalogRunContext\?\.drawingVersion \?\? null/)
  })

  it('CW2 the result card gets the Solar refusal sentence behind the engine flag and Details keeps the raw code', () => {
    const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
    const panel = live.search(/React\.createElement\(\s*ResultPanel,/)
    assert.ok(panel >= 0)
    assert.match(live.slice(panel, panel + 400), /result: shownResult,/)
    assert.match(live, /const shownResult = useMemo\(\s*\(\) => ENV_CAD_EDIT \? solarRefusalEnvelope\(result, selectedTool\) : result,\s*\[result, selectedTool\]\s*\)/)
    const details = live.slice(live.indexOf('const openRunDetails = useCallback'), live.indexOf('const shownResult = useMemo'))
    assert.match(details, /const env = result;/)
  })
})
describe('RAIL overview yields to the expanded job monitor', () => {
  it('RAIL wiring R1 on Solar CAD the drawing overview hides while the expanded job monitor is open on desktop', () => {
    // Comments are removed first, so a commented-out rule is absent and a brace inside a comment cannot end a block.
    const css = readFileSync(new URL('./site/cockpit.css', import.meta.url), 'utf8').split('\r').join('')
      .replace(/\/\*[\s\S]*?\*\//g, '')
    const rule = '.studio-shell .app[data-surface="solar"]:has(aside.rail:not([data-spine])) .cad-overview { display: none; }'
    const panel = '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"]) aside.rail:not([data-spine]) {'
    const blocks = []
    for (let at = css.indexOf('@media (min-width: 981px) {'); at >= 0; at = css.indexOf('@media (min-width: 981px) {', at + 1)) {
      const open = css.indexOf('{', at)
      let depth = 1
      let end = open + 1
      for (; end < css.length && depth > 0; end += 1) {
        if (css[end] === '{') depth += 1
        else if (css[end] === '}') depth -= 1
      }
      assert.equal(depth, 0)
      blocks.push(css.slice(open + 1, end - 1))
    }
    const owner = blocks.filter((block) => block.includes(panel))
    assert.equal(owner.length, 1, 'exactly one desktop block owns the expanded job monitor panel')
    assert.ok(owner[0].includes(rule), 'the overview rule sits in the same desktop block as the expanded job monitor')
    assert.equal(css.split(rule).length - 1, 1, 'the overview rule appears exactly once')
    // The overview sits above the expanded monitor (z 24 over z 5), which is why it must give way.
    const overviewCss = readFileSync(new URL('./site/cadOverview.css', import.meta.url), 'utf8')
    assert.match(overviewCss, /\.cad-overview \{[^}]*z-index: 24;/)
    assert.match(owner[0].split(panel)[1].split('}')[0], /z-index: 5;/)
    // CAD keeps its overview: it reserves the monitor a grid column and moves the overview clear of it, so no
    // cockpit.css rule may hide the overview on CAD while the monitor is open.
    assert.equal(/data-surface="cad"[^{}]*:has\(aside\.rail:not\(\[data-spine\]\)\)[^{}]*\.cad-overview/.test(css), false)
    const shell = readFileSync(new URL('./site/studioShell.css', import.meta.url), 'utf8').split('\r').join('')
    const cadMoves = shell.split('.studio-shell .app[data-studio-shell="cockpit"][data-surface="cad"] :is(.cad-overview, .cockpit-cube-wrap, .cockpit-cube-wcs) {')
    assert.equal(cadMoves.length, 2, 'studioShell.css moves the CAD overview clear of the reserved rail exactly once')
    assert.match(cadMoves[1].split('}')[0], /right: calc\(var\(--ck-rail-width\) \+ 18px\);/)
  })
})

describe('S24 the URL keeps tool, drawer, own selection and camera view', () => {
  const LB = String.fromCharCode(123), RB = String.fromCharCode(125)
  const live = esbuild.transformSync(appSource, { loader: 'jsx' }).code
  const toolCastSource = readFileSync(new URL('./site/ToolCast.jsx', import.meta.url), 'utf8')
  const toolCastLive = esbuild.transformSync(toolCastSource, { loader: 'jsx' }).code
  const urlStateSource = readFileSync(new URL('./lib/urlState.js', import.meta.url), 'utf8')

  it('owns exactly the four allow-listed keys and never round-trips the search through URLSearchParams', () => {
    assert.ok(urlStateSource.includes("export const VIEW_PARAM_KEYS = Object.freeze(['tool', 'drawer', 'sel', 'cam'])"))
    assert.doesNotMatch(urlStateSource.replace(/\/\/[^\n]*/g, ''), /new URLSearchParams/)
    assert.ok(urlStateSource.includes("window.addEventListener('popstate', onChange)"))
    assert.ok(urlStateSource.includes("if (mode === 'push') window.history.pushState(" + LB + RB + ", '', url)"))
    assert.ok(urlStateSource.includes("else window.history.replaceState(window.history.state, '', url)"))
  })

  it('seats the drawer, the opened tool, the own selection and the camera in App (live code, not a comment)', () => {
    assert.match(appSource, /import \x7b pushOnOpen, useCameraViewParam, useViewParamSeat \x7d from \x27\.\/lib\/urlState\.js\x27/)
    for (const call of ["useViewParamSeat('drawer', " + LB, "useViewParamSeat('tool', " + LB, "useViewParamSeat('sel', " + LB]) {
      assert.ok(appSource.includes(call), `App must call ${call}`)
    }
    for (const call of ['useViewParamSeat("drawer"', 'useViewParamSeat("tool"', 'useViewParamSeat("sel"', 'useCameraViewParam(viewerRef']) {
      assert.ok(live.includes(call), `App's compiled code must keep ${call}`)
    }
    assert.ok(appSource.includes("value: drawer?.urlKey === 'details' ? 'details' : studioDrawer === 'none' ? null : studioDrawer,"))
    assert.ok(appSource.includes('value: openTool?.name ?? null,'))
    assert.ok(appSource.includes('value: selectedHandle == null ? null : String(selectedHandle),'))
    const selectionSeat = appSource.slice(appSource.indexOf("useViewParamSeat('sel', " + LB), appSource.indexOf('useCameraViewParam(viewerRef'))
    assert.ok(selectionSeat.includes('!selectEntity(drawingIntake, handle)'), 'URL selections must resolve in the loaded drawing')
    assert.ok(appSource.includes('useCameraViewParam(viewerRef, ' + LB + ' ready: drawingIntake != null ' + RB + ')'))
    const start = appSource.indexOf('const openSessionDetails = useCallback(')
    const body = appSource.slice(start, appSource.indexOf(RB + ', [', start))
    assert.ok(body.includes("urlKey: 'details',"), 'the session Details drawer is the one the URL names')
    // The bounded in-memory default stays; a URL drawer arrives through the seat's restore.
    assert.ok(appSource.includes("const [studioDrawer, setStudioDrawer] = useState('none')"))
  })

  it('seats the same keys in ToolCast only while its scene is active', () => {
    assert.match(toolCastSource, /import \x7b CAM_FOCUS, pushOnOpen, useViewParamSeat \x7d from \x27\.\.\/lib\/urlState\.js\x27/)
    for (const key of ['drawer', 'tool', 'sel', 'cam']) {
      const at = toolCastSource.indexOf(`useViewParamSeat('${key}', ` + LB)
      assert.ok(at >= 0, `ToolCast must seat ${key}`)
      assert.ok(toolCastSource.slice(at, toolCastSource.indexOf('\n  ' + RB + ')', at)).includes('enabled: active,'), `${key} seat is gated on the active scene`)
      assert.ok(toolCastLive.includes(`useViewParamSeat("${key}"`), `ToolCast's compiled code must keep the ${key} seat`)
    }
    assert.ok(toolCastSource.includes('onClick=' + LB + 'openAccountDetails' + RB))
    assert.ok(toolCastSource.includes("urlKey: 'details',"))
    assert.ok(toolCastSource.includes('ready: !busy && !jobRunning && Array.isArray(tools) && tools.length > 0,'), 'pending tool restores wait for the current run')
  })

  it('restores the catalog tool without hiding a resumed authoring request or interrupting a current run', () => {
    const start = toolCastSource.indexOf("useViewParamSeat('tool', " + LB)
    const end = toolCastSource.indexOf("useViewParamSeat('sel', " + LB, start)
    assert.ok(start >= 0 && end > start)
    const seatSource = toolCastSource.slice(start, end)
    const tool = { name: 'count-panels-near-edge' }
    function seat({ pointer = null, busy = false, jobRunning = false } = {}) {
      const selected = [], panels = []
      let config
      const context = {
        useViewParamSeat: (_key, value) => { config = value },
        selectedCatalogTool: null, active: true, busy, jobRunning, tools: [tool],
        authorStage: { pointer }, pushOnOpen: () => 'push',
        setSelectedCatalogTool: (value) => selected.push(value),
        setLeftView: (value) => panels.push(value),
      }
      new Function(...Object.keys(context), seatSource)(...Object.values(context))
      return { config, selected, panels }
    }
    for (const pointer of [null, { target_tool_name: tool.name }, { target_tool_name: tool.name, terminal_staged: true }]) {
      const restored = seat({ pointer })
      assert.equal(restored.config.ready, true)
      assert.equal(restored.config.onRestore(tool.name), true)
      assert.deepEqual(restored.selected, [tool])
      assert.deepEqual(restored.panels, [pointer ? 'author' : 'catalog'])
      assert.equal(restored.config.onRestore('missing-tool'), false)
      assert.equal(restored.config.onRestore(null), true)
      assert.deepEqual(restored.selected, [tool, null])
    }
    for (const state of [{ busy: true }, { jobRunning: true }]) {
      const waiting = seat(state)
      assert.equal(waiting.config.ready, false)
      assert.deepEqual(waiting.selected, [])
      assert.deepEqual(waiting.panels, [])
    }
  })

  it('leaves the router and SiteRoot alone and the drawing param with DrawingIdentityProvider', () => {
    const router = readFileSync(new URL('./site/router.js', import.meta.url), 'utf8')
    const siteRoot = readFileSync(new URL('./site/SiteRoot.jsx', import.meta.url), 'utf8')
    assert.doesNotMatch(router, /urlState/)
    assert.doesNotMatch(siteRoot, /urlState/)
    assert.ok(router.includes('return ' + LB + ' path: window.location.pathname, hash: window.location.hash ' + RB))
    assert.doesNotMatch(urlStateSource, /[\x27\x22]drawing[\x27\x22]/)
  })
})

describe('S23 agent checkpoint toast', () => {
  const LB = '{', RB = '}'
  // The live handler, comments removed (the legacy commented-out copy above
  // it disappears with them), evaluated with spies for every free name.
  function sliceBetween(source, from, to) {
    const start = source.indexOf(from)
    const end = source.indexOf(to, start)
    assert.ok(start >= 0 && end > start, `${from} must survive comment removal`)
    return source.slice(start, end)
  }
  const appHandler = sliceBetween(appNoComments, 'const onAttachAgentJob = useCallback', 'const onAuthor = useCallback')
  function appAttach({ mock = false, envelope }) {
    const h = {
      attachSharedJob: async () => envelope,
      showToast: [], undo: 0, tracked: [], selected: [],
    }
    const context = {
      useCallback: (callback) => callback,
      mock,
      track: (...args) => h.tracked.push(args),
      setSelectedTool: (value) => h.selected.push(value),
      attachSharedJob: (...args) => { h.attachArgs = args; return h.attachSharedJob() },
      showToast: (notice) => h.showToast.push(notice),
      onUndo: async (...args) => { h.undo += 1; h.undoArgs = args },
    }
    h.attach = new Function(...Object.keys(context), appHandler + '\nreturn onAttachAgentJob')(...Object.values(context))
    return h
  }
  const committed = { ok: true, tool: 'drawing.write', result: { new_version: { drawing_id: 'd1', version: 4 } } }

  it('S23-A1 an agent job that committed a version raises one keyed Checkpoint saved notice', async () => {
    const h = appAttach({ envelope: committed })
    assert.equal(await h.attach('job-1', 'drawing.write'), committed)
    assert.deepEqual(h.attachArgs, ['job-1', { toolName: 'drawing.write', persist: true }])
    assert.equal(h.showToast.length, 1)
    const [notice] = h.showToast
    assert.equal(notice.key, 'agent-checkpoint')
    assert.equal(notice.text, 'Checkpoint saved')
    assert.equal(notice.action.label, 'Undo')
  })

  it('S23-A2 the notice Undo runs the ribbon onUndo and nothing else', async () => {
    const h = appAttach({ envelope: committed })
    await h.attach('job-1', 'drawing.write')
    assert.equal(h.undo, 0, 'raising the notice must not undo anything')
    h.showToast[0].action.onClick({ type: 'click' })
    await Promise.resolve()
    assert.equal(h.undo, 1)
    assert.deepEqual(h.undoArgs, [])
  })

  it('S23-A3 no checkpoint for a failed job, a job with no new version, a superseded attach or mock', async () => {
    for (const envelope of [
      { ok: false, result: { new_version: { version: 4 } } },
      { ok: true, result: {} },
      { ok: true },
      null,
    ]) {
      const h = appAttach({ envelope })
      assert.equal(await h.attach('job-1', 'drawing.write'), envelope)
      assert.equal(h.showToast.length, 0, `no checkpoint for ${JSON.stringify(envelope)}`)
    }
    const mocked = appAttach({ mock: true, envelope: committed })
    assert.equal(await mocked.attach('job-1', 'drawing.write'), null)
    assert.equal(mocked.showToast.length, 0)
  })

  it('S23-A4 the handler lists what it reads and onUndo is declared before it', () => {
    assert.match(appHandler, new RegExp('\\' + RB + ', \\[attachSharedJob, mock, onUndo, showToast\\]\\)'))
    const undoAt = appNoComments.indexOf('const onUndo = useCallback')
    assert.ok(undoAt >= 0 && undoAt < appNoComments.indexOf('const onAttachAgentJob = useCallback'))
    assert.ok(stripped.includes('key: "agent-checkpoint"'), 'the keyed notice survives the transform')
  })

  it('S23-A5 the history drawer receives the ribbon Undo and its blocks for Rewind', () => {
    assert.match(appNoComments, new RegExp('<VersionHistory\\s[^>]*onUndo=\\' + LB + 'onUndo\\' + RB))
    assert.match(appNoComments, new RegExp('<VersionHistory\\s[^>]*undoDisabled=\\' + LB + 'versionBusy \\|\\| running \\|\\| !canUndo\\' + RB))
  })

  it('S23-T1 /try raises the same keyed checkpoint and its Undo reaches the bar Undo', async () => {
    // Raw source: the slice evaluated below keeps its comments, which are
    // valid inside the Function body.
    const toolCast = readFileSync(new URL('./site/ToolCast.jsx', import.meta.url), 'utf8')
    const retainedUndo = sliceBetween(toolCast, 'const undoActionRef = useRef(null)', 'const onCompleteVersion = useCallback')
    const handler = sliceBetween(toolCast, 'const attachJob = useCallback', 'const openResultDetails = useCallback')
    const h = { toasts: [], undo: 0 }
    const context = {
      useCallback: (callback) => callback,
      useRef: (value) => ({ current: value }),
      sessionReady: true,
      onJobLinked: () => {},
      attachTrackedJob: async () => committed,
      showToast: (notice) => h.toasts.push(notice),
      setPhase: () => {}, setError: () => {},
      workspace: { rehydrate: async () => {} },
      checkout: { actions: { refresh: () => {} } },
    }
    const made = new Function(...Object.keys(context), retainedUndo + handler + '\nreturn ' + LB + ' attachJob, undoActionRef, onUndo ' + RB)(...Object.values(context))
    made.undoActionRef.current = async () => { h.undo += 1 }
    await made.attachJob('job-1', 'arrange-panels-as-cat')
    assert.equal(h.toasts.length, 1)
    assert.equal(h.toasts[0].key, 'agent-checkpoint')
    assert.equal(h.toasts[0].text, 'Checkpoint saved')
    assert.equal(h.toasts[0].action.label, 'Undo')
    assert.equal(h.toasts[0].action.undo, true)
    assert.equal(h.toasts[0].action.onClick, made.onUndo)
    h.toasts[0].action.onClick()
    assert.equal(h.undo, 1)
    const undoAt = toolCast.indexOf('const undo = useCallback')
    assert.ok(undoAt > 0 && toolCast.indexOf('undoActionRef.current = undo', undoAt) > undoAt, 'the ref follows the bar Undo')
    assert.doesNotMatch(toolCast, /\bundoRef\b/, 'the checkpoint and version toasts share one Undo ref')
    assert.ok(handler.includes('[attachTrackedJob, checkout.actions, onJobLinked, onUndo, sessionReady, showToast, workspace]'))
    const rewindAt = toolCast.indexOf('rewind=' + LB + LB)
    assert.ok(rewindAt > 0 && toolCast.slice(rewindAt, rewindAt + 400).includes('run: () => undo(),'), 'the tab Rewind runs the bar Undo')
  })
})

describe('S20 console chrome', () => {
  const LB = String.fromCharCode(123), RB = String.fromCharCode(125), BT = String.fromCharCode(96)
  it('S20 the drawing line never renders a bare loading literal; it waits on useLoadingPhase and says Loading drawing', () => {
    assert.equal(/[\x27\x22\x60]loading[\x27\x22\x60]/.test(appNoComments), false)
    assert.match(appNoComments, /import \x7b useLoadingPhase \x7d from \x27\.\/lib\/loadingTiming\.js\x27/)
    assert.ok(appNoComments.includes("const drawingLoading = !shown && drawingLoad.state === 'pending'"))
    assert.ok(appNoComments.includes('const drawingLoadPhase = useLoadingPhase(drawingLoading)'))
    assert.ok(appNoComments.includes("const drawingLoadShown = drawingLoading && (drawingLoadPhase === 'shown' || drawingLoadPhase === 'long')"))
    const start = appNoComments.indexOf('<span className="meta">')
    const end = appNoComments.indexOf('</span>', appNoComments.indexOf('Loading drawing', start))
    assert.ok(start >= 0 && end > start)
    const meta = appNoComments.slice(start, end)
    assert.ok(meta.includes(': drawingLoadShown && ('))
    assert.match(meta, /<span className="dot hollow" aria-hidden="true" \/>Loading drawing/)
  })

  it('S20 the Approvals chip shows a dot, not a number, and keeps the counted aria-label', () => {
    const label = 'aria-label=' + LB + BT + 'Pending approvals $' + LB + 'pendingApprovalCount' + RB + BT + RB
    const start = appNoComments.indexOf(label)
    assert.ok(start >= 0, 'the counted aria-label is kept')
    const chip = appNoComments.slice(start, appNoComments.indexOf('</button>', start))
    assert.equal(/\x7bpendingApprovalCount\x7d/.test(chip.slice(label.length)), false, 'no visible count inside the chip')
    assert.equal(/className=\x22key\x22/.test(chip), false)
    assert.ok(chip.includes(LB + 'pendingApprovalCount > 0 && !pendingApprovalsUnavailable && <span className="dot approvals-dot" aria-hidden="true" />' + RB))
    assert.ok(chip.includes(LB + 'pendingApprovalsUnavailable && <span className="dot red approvals-dot" aria-hidden="true" />' + RB))
  })

  it('S20 the nav rail seeds from and writes to the remembered preference', () => {
    assert.match(appNoComments, /import \x7b readNavExpanded, writeNavExpanded \x7d from \x27\.\/lib\/navExpandedPreference\.js\x27/)
    assert.ok(appNoComments.includes('const [navExpanded, setNavExpandedState] = useState(() => readNavExpanded())'))
    const start = appNoComments.indexOf('const setNavExpanded = useCallback((open) => ' + LB)
    const end = appNoComments.indexOf(RB + ', [])', start)
    assert.ok(start >= 0 && end > start)
    const setter = appNoComments.slice(start, end)
    assert.ok(setter.includes('setNavExpandedState(open)'))
    assert.ok(setter.includes('writeNavExpanded(!!open)'))
    // The rewritten comment cites the fork that overrides the rollback contract.
    assert.ok(appSource.includes('REMEMBERED under fork F-studio-rollback-storage (S20)'))
    assert.equal(appSource.includes('IN-MEMORY on purpose'), false)
  })

  it('S20 the jobRail slot passes onRetryJob, which re-dispatches through the confirm path', () => {
    const start = appNoComments.indexOf('jobRail=' + LB + LB)
    const end = appNoComments.indexOf('toast=' + LB, start)
    assert.ok(start >= 0 && end > start)
    assert.match(appNoComments.slice(start, end), /\bonRetryJob,/)
    const defStart = appNoComments.indexOf('const onRetryJob = useCallback((job) => ' + LB)
    const defEnd = appNoComments.indexOf(RB + ', [', defStart)
    assert.ok(defStart >= 0 && defEnd > defStart)
    const def = appNoComments.slice(defStart, defEnd)
    assert.ok(def.includes("job.status !== 'failed'"))
    assert.ok(def.includes('onRequestCatalogRun(tool, sameRun ? last.params : ' + LB + RB + ", null, 'catalog', " + LB + ' complete: sameRun ' + RB + ')'))
  })
})
