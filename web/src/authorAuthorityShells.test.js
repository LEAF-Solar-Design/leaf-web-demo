// C8b: both shells mint fresh author authority on every submission, and a failed turn start
// reaches the drafter instead of being swallowed. Three exceptions give no authority and no
// error: the signed-out demo has no conversation to start, the app console gives nothing to a
// project switched away from, and a conversation busy with another turn starts nothing (the
// controller then says to wait for that turn, as it did before this change).
//
// The provider is a useCallback inside a component far too large to mount here, so each shell's
// source is parsed, its ONE `authorAuthorityProvider` declarator found, and the callback
// evaluated with its lexical names supplied. Every name the callback reads before or after this
// change is supplied, so a run against either tree fails on behaviour, never on a missing name.
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { describe, expect, it } from 'vitest'
import { classifyAgentError } from './converse.js'

const PROVIDER = 'authorAuthorityProvider'

function walk(node, visit) {
  if (!node || typeof node.type !== 'string') return
  visit(node)
  for (const key of Object.keys(node)) {
    const value = node[key]
    if (Array.isArray(value)) for (const child of value) walk(child, visit)
    else if (value && typeof value === 'object') walk(value, visit)
  }
}

function readShell({ name, file, app, demoName, retired, converse }) {
  const source = readFileSync(`${process.cwd()}/src/${file}`, 'utf8')
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
  const text = (node) => source.slice(node.start, node.end)
  const declarators = []
  const seats = []
  const controllers = []
  const imports = {}
  walk(tree, (node) => {
    if (node.type === 'ImportDeclaration') {
      for (const specifier of node.specifiers) {
        if (specifier.type === 'ImportSpecifier') {
          imports[specifier.local.name] = `${text(specifier.imported)} from ${node.source.value}`
        }
      }
    }
    if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier' && node.id.name === PROVIDER) {
      declarators.push(node)
    }
    if (node.type === 'JSXOpeningElement') {
      for (const attribute of node.attributes) {
        const expression = attribute.type === 'JSXAttribute' && attribute.value?.type === 'JSXExpressionContainer'
          ? attribute.value.expression
          : null
        if (expression?.type === 'Identifier' && expression.name === PROVIDER) {
          seats.push(`${text(node.name)}.${attribute.name.name}`)
        }
      }
    }
    if (node.type === 'CallExpression' && node.callee.type === 'Identifier' && node.callee.name === 'useAuthorStageController') {
      const properties = {}
      for (const property of node.arguments[0]?.properties || []) {
        if (property.type === 'ObjectProperty' && property.key.type === 'Identifier') {
          properties[property.key.name] = text(property.value)
        }
      }
      controllers.push(properties)
    }
  })
  if (declarators.length !== 1) throw new Error(`${file}: ${declarators.length} ${PROVIDER} declarators, expected one`)
  const call = declarators[0].init
  if (call?.type !== 'CallExpression' || call.callee.name !== 'useCallback' || call.arguments.length !== 2
      || call.arguments[1].type !== 'ArrayExpression') {
    throw new Error(`${file}: ${PROVIDER} is not a useCallback with a literal dependency list`)
  }
  return {
    name,
    app,
    demoName,
    retired,
    source,
    callbackSource: text(call.arguments[0]),
    deps: call.arguments[1].elements.map(text),
    seats,
    controllers,
    imports,
    converse,
  }
}

const SHELLS = [
  readShell({
    name: 'App',
    file: 'App.jsx',
    app: true,
    demoName: 'mock',
    converse: './converse.js',
    retired: ['AUTHOR_AUTHORITY_TTL_MS', 'authorAuthorityRef', 'agentSessionIdRef'],
  }),
  readShell({
    name: 'ToolCast',
    file: 'site/ToolCast.jsx',
    app: false,
    demoName: 'PUBLIC_DEMO',
    converse: '../converse.js',
    retired: ['AUTHOR_AUTHORITY_TTL_MS', 'authorAuthorityRef'],
  }),
]

const turn = (n) => ({ session_id: 'session-live', turn_id: `turn-${n}` })
const authority = (n) => ({ sessionId: 'session-live', turnId: `turn-${n}` })

function evaluate(callbackSource, scope) {
  const names = Object.keys(scope)
  return Function(...names, `'use strict'; return (${callbackSource}\n)`)(...names.map((key) => scope[key]))
}

// `staleSession` gives the state-fed session a value the response never carries. Without it the
// two agree, which is the case the retired cache needed in order to answer from memory.
function harness(shell, { demo = false, project = 'project-1', staleSession = false } = {}) {
  const calls = []
  let behave = (n) => turn(n)
  const start = async (...args) => {
    calls.push(args)
    return behave(calls.length)
  }
  const authorProjectRef = { current: project }
  const startTurnRef = { current: start }
  const scope = shell.app
    ? {
        startAgentTurn: start,
        classifyAgentError,
        authorProjectRef,
        mock: demo,
        authorAuthorityRef: { current: null },
        agentSessionIdRef: { current: staleSession ? 'session-stale' : 'session-live' },
        AUTHOR_AUTHORITY_TTL_MS: 120_000,
      }
    : {
        startTurnRef,
        classifyAgentError,
        PUBLIC_DEMO: demo,
        authorAuthorityRef: { current: null },
        AUTHOR_AUTHORITY_TTL_MS: 120_000,
      }
  return {
    provider: evaluate(shell.callbackSource, scope),
    calls,
    authorProjectRef,
    startTurnRef,
    onStart: (next) => { behave = next },
  }
}

async function outcome(promise) {
  try {
    return { value: await promise }
  } catch (error) {
    return { error }
  }
}

// The refusal the conversation client throws when the session already has an active turn: the
// server's 409 with its own sentence and code, tagged the way converse.js tags every refusal.
function busyRefusal(code = 'TURN_IN_PROGRESS') {
  const message = "session 'session-live' already has an active turn"
  return Object.assign(new Error(message), {
    status: 409,
    errorCode: code,
    body: { ok: false, error: { error_code: code, message, retryable: true } },
  })
}

// Refusals that look like a busy conversation and are not one. Each must reach the caller.
const NOT_BUSY = [
  ['a 409 with another code', () => Object.assign(new Error('approval not resolvable'), { status: 409, errorCode: 'BAD_PARAMS' })],
  ['a 409 with no code', () => Object.assign(new Error('conflict'), { status: 409, errorCode: null })],
  ['a code that only contains the busy code', () => Object.assign(new Error('nearly'), { status: 409, errorCode: 'TURN_IN_PROGRESS_X' })],
  ['the busy sentence with no code', () => Object.assign(new Error("session 'session-live' already has an active turn"), { status: 409 })],
  ['the busy code on the body only', () => Object.assign(new Error('conflict'), { status: 409, body: { error: { error_code: 'TURN_IN_PROGRESS' } } })],
]

const FAILURES = [
  ['a grant refusal', () => Object.assign(new Error('grant required'), { status: 403, reasonCode: 'provider_grant_required' })],
  ['a provider quota refusal', () => Object.assign(new Error('quota exhausted'), { status: 429, reasonCode: 'provider_quota_exhausted' })],
  ['an expired sign-in', () => Object.assign(new Error('unauthenticated'), { status: 401 })],
  ['a network failure', () => new TypeError('Failed to fetch')],
  ['a frozen error', () => Object.freeze(new Error('frozen'))],
]

for (const shell of SHELLS) {
  describe(`${shell.name} author authority`, () => {
    it(`C8B-01 ${shell.name}: a second submission moments later mints a second turn`, async () => {
      const h = harness(shell)
      expect(await h.provider('describe the tool')).toEqual(authority(1))
      expect(await h.provider('describe the tool')).toEqual(authority(2))
      expect(h.calls).toHaveLength(2)
    })

    it(`C8B-02 ${shell.name}: a failed start reaches the caller as the same error`, async () => {
      for (const [label, make] of FAILURES) {
        const h = harness(shell)
        const failure = make()
        h.onStart(() => { throw failure })
        const result = await outcome(h.provider('describe the tool'))
        expect(result.error, label).toBe(failure)
        expect(h.calls, label).toHaveLength(1)
      }
    })

    it(`C8B-03 ${shell.name}: a failed start is not remembered`, async () => {
      const h = harness(shell)
      const failure = new Error('first start failed')
      h.onStart((n) => {
        if (n === 1) throw failure
        return turn(n)
      })
      expect((await outcome(h.provider('describe the tool'))).error).toBe(failure)
      expect(await h.provider('describe the tool')).toEqual(authority(2))
      expect(h.calls).toHaveLength(2)
    })

    it(`C8B-04 ${shell.name}: in the signed-out demo a failed start returns no authority`, async () => {
      const h = harness(shell, { demo: true })
      h.onStart(() => { throw Object.assign(new Error('unauthenticated'), { status: 401 }) })
      expect(await outcome(h.provider('describe the tool'))).toEqual({ value: null })
      expect(await outcome(h.provider('describe the tool'))).toEqual({ value: null })
      expect(h.calls).toHaveLength(2)
    })

    it(`C8B-05 ${shell.name}: the demo flag changes only what a failure looks like`, async () => {
      const h = harness(shell, { demo: true })
      expect(await h.provider('describe the tool')).toEqual(authority(1))
      expect(h.calls).toHaveLength(1)
    })

    if (shell.app) {
      it('C8B-06 App: a project named by the caller must be the open one', async () => {
        const other = harness(shell)
        expect(await other.provider('describe the tool', { projectId: 'project-2' })).toBeNull()
        expect(other.calls).toHaveLength(0)

        const same = harness(shell)
        expect(await same.provider('describe the tool', { projectId: 'project-1' })).toEqual(authority(1))

        for (const projectId of [null, '']) {
          const none = harness(shell, { project: null })
          expect(await none.provider('describe the tool', { projectId }), String(projectId)).toEqual(authority(1))
          const named = harness(shell)
          expect(await named.provider('describe the tool', { projectId }), String(projectId)).toBeNull()
          expect(named.calls, String(projectId)).toHaveLength(0)
        }
      })

      it('C8B-07 App: a project change while the turn starts gets no authority', async () => {
        const h = harness(shell)
        h.onStart((n) => {
          h.authorProjectRef.current = 'project-2'
          return turn(n)
        })
        expect(await h.provider('describe the tool')).toBeNull()
        expect(h.calls).toHaveLength(1)
      })

      it('C8B-08 App: a project change while the start fails gets no authority and no error', async () => {
        const h = harness(shell)
        h.onStart(() => {
          h.authorProjectRef.current = 'project-2'
          throw new Error('failed after the switch')
        })
        expect(await outcome(h.provider('describe the tool'))).toEqual({ value: null })
        expect(h.calls).toHaveLength(1)
      })
    } else {
      it('C8B-16 ToolCast: each submission reads the current start function', async () => {
        const h = harness(shell)
        expect(await h.provider('describe the tool')).toEqual(authority(1))
        const later = []
        h.startTurnRef.current = async (...args) => {
          later.push(args)
          return { session_id: 'session-later', turn_id: 'turn-later' }
        }
        expect(await h.provider('describe the tool')).toEqual({ sessionId: 'session-later', turnId: 'turn-later' })
        expect(h.calls).toHaveLength(1)
        expect(later).toHaveLength(1)
      })
    }

    it(`C8B-09 ${shell.name}: the authority is the response's own session and turn`, async () => {
      const h = harness(shell, { staleSession: true })
      h.onStart(() => ({ session_id: 'session-fresh', turn_id: 'turn-fresh', status: 'running' }))
      const result = await h.provider('describe the tool')
      expect(result).toEqual({ sessionId: 'session-fresh', turnId: 'turn-fresh' })
      expect(Object.keys(result).sort()).toEqual(['sessionId', 'turnId'])
    })

    it(`C8B-10 ${shell.name}: an incomplete response gets no authority and is not remembered`, async () => {
      const incomplete = [
        null,
        undefined,
        {},
        { session_id: 'session-live' },
        { turn_id: 'turn-1' },
        { session_id: '', turn_id: 'turn-1' },
        { session_id: 'session-live', turn_id: '' },
      ]
      for (const response of incomplete) {
        const h = harness(shell)
        h.onStart((n) => (n === 1 ? response : turn(n)))
        expect(await h.provider('describe the tool'), JSON.stringify(response)).toBeNull()
        expect(await h.provider('describe the tool'), JSON.stringify(response)).toEqual(authority(2))
      }
    })

    it(`C8B-11 ${shell.name}: the credential override and the immediate-turn rule reach the one start`, async () => {
      const allowed = harness(shell)
      await allowed.provider('describe the tool', { allowSecretOnce: true, forceFresh: true })
      expect(allowed.calls).toEqual([[
        'describe the tool',
        { source: 'author_panel', purpose: 'stage_authority' },
        { allowSecretOnce: true, requireImmediateTurn: true },
      ]])

      const plain = harness(shell)
      await plain.provider('another description')
      expect(plain.calls).toEqual([[
        'another description',
        { source: 'author_panel', purpose: 'stage_authority' },
        { allowSecretOnce: false, requireImmediateTurn: true },
      ]])
    })

    it(`C8B-12 ${shell.name}: nothing remembers an earlier turn`, () => {
      for (const name of shell.retired) expect(shell.source.includes(name), name).toBe(false)
      expect(shell.callbackSource.includes('forceFresh')).toBe(false)
      expect(/\bDate\b|\bperformance\b|\bsetTimeout\b/.test(shell.callbackSource)).toBe(false)
    })

    it(`C8B-13 ${shell.name}: the callback is rebuilt when what it reads changes`, () => {
      expect(shell.deps).toEqual(shell.app ? ['mock', 'startAgentTurn'] : [])
    })

    it(`C8B-14 ${shell.name}: the author controller and its demo flag are wired to this provider`, () => {
      expect(shell.controllers).toHaveLength(1)
      expect(shell.controllers[0].authorityProvider).toBe(PROVIDER)
      expect(shell.controllers[0].mock).toBe(shell.demoName)
      expect(shell.seats).toEqual(shell.app
        ? ['CampaignPanel.authorityProvider', 'CampaignPanel.authorityProvider']
        : [])
    })

    it(`C8B-17 ${shell.name}: a busy conversation gets no authority and no error`, async () => {
      for (const code of ['TURN_IN_PROGRESS', 'turn_in_progress']) {
        const h = harness(shell)
        h.onStart((n) => {
          if (n === 1) throw busyRefusal(code)
          return turn(n)
        })
        expect(await outcome(h.provider('describe the tool')), code).toEqual({ value: null })
        // Nothing is remembered about the busy answer: once that turn has ended the next
        // submission starts its own.
        expect(await h.provider('describe the tool'), code).toEqual(authority(2))
        expect(h.calls, code).toHaveLength(2)
      }
    })

    it(`C8B-18 ${shell.name}: only a busy conversation is treated as busy`, async () => {
      for (const [label, make] of NOT_BUSY) {
        const h = harness(shell)
        const failure = make()
        h.onStart(() => { throw failure })
        const result = await outcome(h.provider('describe the tool'))
        expect(result.error, label).toBe(failure)
        expect(h.calls, label).toHaveLength(1)
      }
    })

    // The rows above hand the callback its names, so they pass whether or not the shell binds
    // them. This row reads the shell's own import: the classifier the callback calls is the
    // conversation client's, under its own name, and no local name hides it.
    it(`C8B-19 ${shell.name}: the shell imports the busy classifier its provider calls`, () => {
      expect(shell.imports.classifyAgentError).toBe(`classifyAgentError from ${shell.converse}`)
      expect(shell.callbackSource).toContain("classifyAgentError(error) === 'busy'")
      expect(shell.source.match(/\b(?:const|let|var|function)\s+classifyAgentError\b/g)).toBeNull()
    })

    it(`C8B-15 ${shell.name}: two submissions started together each mint their own turn`, async () => {
      const h = harness(shell)
      let release
      const gate = new Promise((resolve) => { release = resolve })
      h.onStart(async (n) => {
        await gate
        return turn(n)
      })
      const first = h.provider('one')
      const second = h.provider('two')
      expect(h.calls.map((args) => args[0])).toEqual(['one', 'two'])
      release()
      expect(await first).toEqual(authority(1))
      expect(await second).toEqual(authority(2))
    })
  })
}
