import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  appendPromptHistory,
  autoGrowHeight,
  createPromptHistoryState,
  filterRunnable,
  historyKeydown,
  promptHistoryFor,
  rankEntries,
  setPromptHistoryValue,
  LINE_PX,
  MAX_ROWS,
} from './composer.js'

const names = (entries) => entries.map((e) => e.name)

describe('autoGrowHeight', () => {
  it('leaves the single-line well to CSS', () => {
    assert.equal(autoGrowHeight(''), undefined)
    assert.equal(autoGrowHeight('one line'), undefined)
    assert.equal(autoGrowHeight(null), undefined)
    assert.equal(autoGrowHeight(undefined), undefined)
  })

  it('grows by line count once a newline exists', () => {
    assert.equal(autoGrowHeight('a\nb'), `${2 * LINE_PX}px`)
    assert.equal(autoGrowHeight('a\nb\nc'), `${3 * LINE_PX}px`)
  })

  it('counts a trailing newline as the row the caret sits on', () => {
    // Pressing Shift+Enter at the end must open the row you are about to type
    // into, so the caret is never hidden below the visible well.
    assert.equal(autoGrowHeight('a\n'), `${2 * LINE_PX}px`)
  })

  it('caps growth so a pasted wall of text scrolls instead of eating the viewport', () => {
    const many = Array.from({ length: MAX_ROWS + 40 }, (_, i) => `line ${i}`).join('\n')
    assert.equal(autoGrowHeight(many), `${MAX_ROWS * LINE_PX}px`)
  })
})

describe('rankEntries', () => {
  const tools = [
    { name: 'count-panels', description: 'count the panels' },
    { name: 'measure-roof', description: 'measure a roofline' },
    { name: 'panel-report', description: 'summarise count-panels output' },
  ]

  it('ranks prefix matches ahead of substring matches', () => {
    assert.deepEqual(names(rankEntries(tools, 'panel')), ['panel-report', 'count-panels'])
  })

  it('matches on description as well as name', () => {
    assert.deepEqual(names(rankEntries(tools, 'roofline')), ['measure-roof'])
  })

  it('returns everything for an empty query, in server order', () => {
    // Every name startsWith('') so all three are prefix matches: the picker
    // opening on a bare "/" must show the catalog as the server ordered it.
    assert.deepEqual(names(rankEntries(tools, '')), ['count-panels', 'measure-roof', 'panel-report'])
  })

  it('preserves server order for kind-less entries (behaviour parity)', () => {
    // THE regression guard for wiring this into PromptBox: today's payload
    // carries no `kind`, so every entry shares one group rank and the stable
    // sort must leave the previous inline ranking untouched.
    const q = 'p'
    const previousInline = () => {
      const pre = []
      const sub = []
      for (const t of tools) {
        const name = t.name.toLowerCase()
        if (name.startsWith(q)) pre.push(t)
        else if (name.includes(q) || t.description.toLowerCase().includes(q)) sub.push(t)
      }
      return [...pre, ...sub]
    }
    assert.deepEqual(names(rankEntries(tools, q)), names(previousInline()))
  })

  it('groups command before skill before tool once kinds arrive', () => {
    const mixed = [
      { name: 'panel-tool', kind: 'tool' },
      { name: 'panel-skill', kind: 'skill' },
      { name: 'panel-command', kind: 'command' },
    ]
    assert.deepEqual(names(rankEntries(mixed, 'panel')), ['panel-command', 'panel-skill', 'panel-tool'])
  })

  it('sorts unknown kinds last rather than dropping them', () => {
    const mixed = [
      { name: 'panel-mystery', kind: 'mcp_prompt' },
      { name: 'panel-command', kind: 'command' },
    ]
    assert.deepEqual(names(rankEntries(mixed, 'panel')), ['panel-command', 'panel-mystery'])
  })

  it('is safe on absent input', () => {
    assert.deepEqual(rankEntries(undefined, 'x'), [])
    assert.deepEqual(rankEntries([], 'x'), [])
    assert.deepEqual(names(rankEntries([{ name: 'a' }], undefined)), ['a'])
  })
})

describe('filterRunnable', () => {
  const actions = { help: () => {} }
  const entries = [
    { kind: 'command', name: 'help', client_action: 'help' },
    { kind: 'command', name: 'stop', client_action: 'cancel_turn' },
    { kind: 'command', name: 'broken', client_action: 'nope' },
    { kind: 'command', name: 'actionless' },
    { kind: 'skill', name: 'orwell-writing' },
    { kind: 'tool', name: 'count_panels' },
  ]

  it('drops commands the client cannot dispatch — no dead affordances', () => {
    assert.deepEqual(names(filterRunnable(entries, actions)),
      ['help', 'orwell-writing', 'count_panels'])
  })

  it('keeps a command as soon as its handler exists', () => {
    const withStop = { ...actions, cancel_turn: () => {} }
    assert.ok(names(filterRunnable(entries, withStop)).includes('stop'))
  })

  it('never filters tools or skills — they use the existing dispatch path', () => {
    assert.deepEqual(names(filterRunnable(entries, {})), ['orwell-writing', 'count_panels'])
  })

  it('is safe on absent or malformed input', () => {
    assert.deepEqual(filterRunnable(undefined, actions), [])
    assert.deepEqual(filterRunnable([null, {}, { name: '' }], actions), [])
  })

  it('rejects a non-function handler (a truthy value is not dispatchable)', () => {
    assert.deepEqual(names(filterRunnable(entries, { help: 'yes' })),
      ['orwell-writing', 'count_panels'])
  })
})

describe('prompt history', () => {
  const send = (state, text, sessionId = state.sessionId) => appendPromptHistory(state, text, sessionId)
  const key = (state, keyName, value, selectionStart, sessionId = state.sessionId) => historyKeydown(state, {
    key: keyName, value, selectionStart, sessionId,
  })

  it('recalls newest first from an empty composer and stops at the oldest entry', () => {
    let state = createPromptHistoryState('session-a')
    state = send(state, 'first prompt')
    state = send(state, 'newest prompt')

    let result = key(state, 'ArrowUp', '', 0)
    assert.equal(result.handled, true)
    assert.equal(result.value, 'newest prompt')

    result = key(result.state, 'ArrowUp', result.value, result.selectionStart)
    assert.equal(result.value, 'first prompt')

    result = key(result.state, 'ArrowUp', result.value, result.selectionStart)
    assert.equal(result.handled, true)
    assert.equal(result.value, 'first prompt')
  })

  it('leaves ArrowUp to the textarea when the caret is not on its first line', () => {
    let state = createPromptHistoryState('session-a')
    state = send(state, 'older prompt')
    const value = 'line one\nline two'
    const result = key(state, 'ArrowUp', value, value.length)
    assert.equal(result.handled, false)
    assert.equal(result.value, value)
  })

  it('restores the exact pre-navigation draft when Down moves past newest history', () => {
    let state = createPromptHistoryState('session-a')
    state = send(state, 'first prompt')
    state = send(state, 'newest prompt')
    const draft = 'two\nline draft'

    let result = key(state, 'ArrowUp', draft, 0)
    result = key(result.state, 'ArrowDown', result.value, result.selectionStart)
    assert.equal(result.value, draft)
    assert.equal(result.state.historyIndex, null)
  })

  it('appends sent prompts, resets navigation, and never mutates recalled history', () => {
    let state = createPromptHistoryState('session-a')
    state = send(state, 'original')
    let result = key(state, 'ArrowUp', '', 0)
    state = setPromptHistoryValue(result.state, 'edited recall')
    state = send(state, 'next prompt')

    assert.equal(state.historyIndex, null)
    assert.deepEqual(promptHistoryFor(state), ['original', 'next prompt'])
  })

  it('keeps different session histories separate', () => {
    let state = createPromptHistoryState('session-a')
    state = send(state, 'only in a')
    state = send(state, 'only in b', 'session-b')

    const a = key(state, 'ArrowUp', '', 0, 'session-a')
    assert.equal(a.value, 'only in a')
    const b = key(a.state, 'ArrowUp', '', 0, 'session-b')
    assert.equal(b.value, 'only in b')
    const c = key(b.state, 'ArrowUp', '', 0, 'session-c')
    assert.equal(c.handled, false)
  })
})
