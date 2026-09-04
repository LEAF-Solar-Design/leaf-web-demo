import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it } from 'node:test'

import esbuild from 'esbuild'

import {
  acceptQueuedTurn,
  appendPromptHistory,
  autoGrowHeight,
  createPromptHistoryState,
  createQueuedTurnState,
  filterRunnable,
  historyKeydown,
  mergePickerEntries,
  mergeSkillEntries,
  pickerTrigger,
  promptHistoryFor,
  rankEntries,
  reconcileQueuedTurn,
  replacePickerTrigger,
  shouldRetryWithQueue,
  chooseQuestionOption,
  clearSendingQuestion,
  questionChoiceState,
  setPromptHistoryValue,
  LINE_PX,
  MAX_ROWS,
  MAX_SEEN_QUEUED_STARTS,
  MAX_IMAGES_PER_MESSAGE,
  MAX_IMAGE_BYTES,
  base64SizeForBytes,
  clipboardImagesToAttachments,
  thumbnailImages,
} from './composer.js'

const names = (entries) => entries.map((e) => e.name)
const promptBoxSource = readFileSync(new URL('./components/PromptBox.jsx', import.meta.url), 'utf8')
const promptBoxStripped = esbuild.transformSync(promptBoxSource, { loader: 'jsx' }).code
const strip = (relative) => esbuild.transformSync(
  readFileSync(new URL(relative, import.meta.url), 'utf8'),
  { loader: 'jsx' },
).code
// Every source file under web/src, comments and string bodies intact but JSX
// stripped, addressed by its path relative to src/. The census pins below walk
// this list rather than a hand-kept file list, so a NEW file that sends free
// text is caught the moment it appears.
const SRC_ROOT = fileURLToPath(new URL('.', import.meta.url))
const IS_TEST = /\.(test|spec)\.[cm]?[jt]sx?$/
function listSrcFiles(dir = SRC_ROOT, prefix = '') {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) {
      if (entry.name === 'assets') continue
      out.push(...listSrcFiles(join(dir, entry.name), rel))
    } else if (/\.[cm]?[jt]sx?$/.test(entry.name) && !IS_TEST.test(entry.name)) {
      out.push(rel)
    }
  }
  return out.sort()
}
const SRC_FILES = listSrcFiles()
const srcCache = new Map()
const readSrc = (rel) => {
  if (!srcCache.has(rel)) srcCache.set(rel, readFileSync(join(SRC_ROOT, rel), 'utf8'))
  return srcCache.get(rel)
}
// Comment-stripped, for pins on identifiers: a comment that merely NAMES a
// deleted latch must not red a gate, and a comment claiming a property must
// not satisfy one. Falls back to the raw text if a file will not transform,
// which fails in the strict direction (the pin still sees everything).
// Whitespace-insensitive comparison, so a reformat cannot move a pin.
const bare = (text) => String(text).replace(/\s+/g, '')
const strippedCache = new Map()
const readStripped = (rel) => {
  if (!strippedCache.has(rel)) {
    let out
    try {
      // minifyWhitespace is what actually removes comments; a plain
      // transform keeps them, which would let a comment satisfy a pin.
      out = esbuild.transformSync(readSrc(rel), { loader: 'jsx', minifyWhitespace: true }).code
    } catch {
      out = readSrc(rel)
    }
    strippedCache.set(rel, out)
  }
  return strippedCache.get(rel)
}
// Only a module that can issue a request can be a second sender. A mock or a
// doc comment naming an endpoint is not one, and counting it would push this
// census toward being switched off.
const SENDERS = SRC_FILES.filter((file) => /\bfetch\(/.test(readSrc(file)))

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

describe('image attachments', () => {
  const item = (type = 'image/png', size = 12) => ({ kind: 'file', type, getAsFile: () => ({ type, size }) })

  it('extracts supported clipboard images and leaves non-images alone', () => {
    const result = clipboardImagesToAttachments([item(), { kind: 'string', type: 'text/plain' }])
    assert.equal(result.error, null)
    assert.deepEqual(result.attachments.map((image) => image.media_type), ['image/png'])
  })

  it('refuses image count and byte caps instead of silently truncating', () => {
    assert.match(clipboardImagesToAttachments(Array.from({ length: MAX_IMAGES_PER_MESSAGE + 1 }, () => item())).error, /At most/)
    assert.match(clipboardImagesToAttachments([item('image/jpeg', MAX_IMAGE_BYTES + 1)]).error, /1MB/)
    assert.equal(base64SizeForBytes(3), 4)
  })

  it('folds validated image data into capped thumbnail URLs', () => {
    const images = Array.from({ length: MAX_IMAGES_PER_MESSAGE + 1 }, () => ({ media_type: 'image/png', data: 'aGVsbG8=' }))
    assert.deepEqual(thumbnailImages(images), Array.from({ length: MAX_IMAGES_PER_MESSAGE }, () => 'data:image/png;base64,aGVsbG8='))
  })

  it('wires paste handling and clear-after-send attachment state into the composers', () => {
    assert.match(promptBoxStripped, /onPaste,/)
    const panel = readFileSync(new URL('./components/ConversePanel.jsx', import.meta.url), 'utf8')
    assert.match(panel, /const clearAttachments = \(\) => setAttachments/)
    assert.match(panel, /URL\.revokeObjectURL/)
    assert.match(panel, /images\.length \? \{ images \}/)
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

describe('skill entry sources', () => {
  it('merges fetched skills while preserving registry skills as the source of truth', () => {
    const registry = [
      { kind: 'command', name: 'help', description: 'registry command' },
      { kind: 'skill', name: 'orwell-writing', description: 'registry description' },
    ]
    const fetched = [
      { name: 'orwell-writing', description: 'stale fetched description' },
      { name: 'roof-analysis', description: 'analyse roof geometry' },
    ]

    assert.deepEqual(mergeSkillEntries(registry, fetched), [
      ...registry,
      { kind: 'skill', name: 'roof-analysis', description: 'analyse roof geometry' },
    ])
  })
})

describe('MCP picker sources', () => {
  it('merges resource roots and preserves their exact mention insertion text', () => {
    assert.deepEqual(mergePickerEntries(
      [{ kind: 'command', name: 'help' }], [], [{ name: 'roof-mcp', host: 'mcp.example.test' }],
      [{ kind: 'command', name: 'mcp', client_action: 'mcp' }],
    ), [
      { kind: 'command', name: 'help' },
      { kind: 'command', name: 'mcp', client_action: 'mcp' },
      { kind: 'resource', name: 'roof-mcp', description: 'mcp.example.test', insertionText: '@roof-mcp:' },
    ])
  })
})

describe('pickerTrigger', () => {
  it('opens a resource picker only at a word boundary and ignores email interiors', () => {
    assert.deepEqual(pickerTrigger('@roof'), { kind: 'resource', query: 'roof', start: 0, end: 5 })
    assert.deepEqual(pickerTrigger('use @roof'), { kind: 'resource', query: 'roof', start: 4, end: 9 })
    assert.equal(pickerTrigger('a@b'), null)
  })

  it('uses the caret and stays inert while an IME composes', () => {
    assert.deepEqual(pickerTrigger('@roof later', 5), { kind: 'resource', query: 'roof', start: 0, end: 5 })
    assert.equal(pickerTrigger('@roof', 5, true), null)
    const slashWithArgs = '/tool args'
    assert.equal(pickerTrigger(slashWithArgs, 2), null)
    assert.equal(slashWithArgs, '/tool args')
    assert.equal(replacePickerTrigger('@roof later', { start: 0, end: 5 }, '@roof-mcp:'),
      '@roof-mcp: later')

    assert.match(promptBoxStripped, /pickerTrigger\(value, caret, isComposing\)/)
    assert.match(promptBoxStripped, /replacePickerTrigger\(value, trigger, insertion\)/)
    const unwired = promptBoxSource.replace(
      'pickerTrigger(value, caret, isComposing)',
      'pickerTrigger(value, caret)',
    )
    assert.notEqual(unwired, promptBoxSource, 'the falsification mutation must target picker IME wiring')
    assert.doesNotMatch(esbuild.transformSync(unwired, { loader: 'jsx' }).code,
      /pickerTrigger\(value, caret, isComposing\)/)
  })
})

describe('busy queue retry', () => {
  it('retries only a busy plain-text send without a credential grant or confirmation', () => {
    assert.equal(shouldRetryWithQueue('busy', { text: 'continue' }), true)
    assert.equal(shouldRetryWithQueue('rate_limited', { text: 'continue' }), false)
    assert.equal(shouldRetryWithQueue('busy', { confirm: { confirmationId: 'c1' } }), false)
    assert.equal(shouldRetryWithQueue('busy', {
      text: 'continue', credential_grant: { kind: 'api_key' },
    }), false)
    assert.equal(shouldRetryWithQueue('busy', {
      text: 'continue', images: [{ media_type: 'image/png' }],
    }), false)
  })
})

describe('structured question choices', () => {
  const question = {
    type: 'question_required', turn_id: 'turn-question',
    data: { question_id: 'question-1', question: 'Which plan?', options: [{ label: 'Standard' }, { label: 'Premium' }] },
  }

  it('resolves by the FIRST subsequent turn and remembers WHICH label', () => {
    assert.deepEqual(questionChoiceState([question], 'question-1'),
      { answered: false, selectedLabel: null, dismissed: false })
    assert.deepEqual(questionChoiceState([question, {
      type: 'turn_started', turn_id: 'turn-answer', data: { text: 'Standard' },
    }], 'question-1'), { answered: true, selectedLabel: 'Standard', dismissed: false })
  })

  it('a LATER unrelated message matching a label cannot retro-answer the card', () => {
    // Review round 2: scanning every later turn let an ordinary "Premium"
    // typed much later invent a historical selection on replay. Only the
    // first subsequent turn resolves.
    const state = questionChoiceState([question,
      { type: 'turn_started', turn_id: 'turn-next', data: { text: 'unrelated question about panels' } },
      { type: 'turn_started', turn_id: 'turn-later', data: { text: 'Premium' } },
    ], 'question-1')
    assert.deepEqual(state, { answered: false, selectedLabel: null, dismissed: true })
    // dismissed cards are inert to clicks
    assert.equal(chooseQuestionOption(undefined, [question,
      { type: 'turn_started', turn_id: 'turn-next', data: { text: 'moved on' } },
    ], 'question-1', 'Premium').action, 'ignore')
  })

  it('a confirm-only turn DISMISSES; a later matching message cannot retro-answer', () => {
    // Review round 3: confirm turns carry `confirm`, not `text`. Skipping
    // them let `confirm -> later "Standard"` invent a selection.
    const state = questionChoiceState([question,
      { type: 'turn_started', turn_id: 'turn-confirm', data: { confirm: { confirmation_id: 'c1', approved: true } } },
      { type: 'turn_started', turn_id: 'turn-later', data: { text: 'Standard' } },
    ], 'question-1')
    assert.deepEqual(state, { answered: false, selectedLabel: null, dismissed: true })
  })

  it('a prompt QUEUED before the question neither answers nor dismisses it', () => {
    // Review round 3: the queued prompt was authored before the user saw the
    // question — its promoted turn (correlated by queued_id) is not a reply.
    const events = [
      { type: 'turn_queued', turn_id: null, data: { queued_id: 'q-early', text: 'earlier ask' } },
      question,
      { type: 'turn_started', turn_id: 'turn-promoted', data: { text: 'earlier ask', queued_id: 'q-early' } },
      { type: 'turn_started', turn_id: 'turn-real', data: { text: 'Premium' } },
    ]
    assert.deepEqual(questionChoiceState(events, 'question-1'),
      { answered: true, selectedLabel: 'Premium', dismissed: false })
  })

  it('a prompt queued AFTER the question resolves it normally', () => {
    const events = [
      question,
      { type: 'turn_queued', turn_id: null, data: { queued_id: 'q-late', text: 'Standard' } },
      { type: 'turn_started', turn_id: 'turn-late', data: { text: 'Standard', queued_id: 'q-late' } },
    ]
    assert.deepEqual(questionChoiceState(events, 'question-1'),
      { answered: true, selectedLabel: 'Standard', dismissed: false })
  })

  it('sends the option label once and then makes a repeat click inert', () => {
    const first = chooseQuestionOption(undefined, [question], 'question-1', 'Premium')
    assert.equal(first.action, 'send')
    assert.equal(first.text, 'Premium')
    assert.deepEqual(chooseQuestionOption(first.state, [question], 'question-1', 'Premium'), {
      action: 'ignore', state: first.state,
    })
  })

  it('releases a failed answer POST so the question can be retried', () => {
    assert.deepEqual(clearSendingQuestion({ sendingQuestionIds: ['question-1', 'question-2'] }, 'question-1'), {
      sendingQuestionIds: ['question-2'],
    })
  })
})

describe('queued turn reconciliation', () => {
  const queuedTurn = { queuedId: 'queued-1', text: 'continue with the panel count' }

  it('shows the queue first, then promotes only its matching queued_id', () => {
    const queued = acceptQueuedTurn(createQueuedTurnState(), queuedTurn)
    assert.equal(queued.action, 'queue')
    const promoted = reconcileQueuedTurn(queued.state, {
      type: 'turn_started',
      turn_id: 'turn-9',
      data: { queued_id: 'queued-1', text: 'server-normalized text' },
    })
    assert.equal(promoted.action, 'promote')
    assert.deepEqual(promoted.turn, { turnId: 'turn-9', text: 'server-normalized text' })
    assert.equal(promoted.state.queuedTurn, null)
  })

  it('promotes immediately when turn_started arrives before its queued 202', () => {
    const started = reconcileQueuedTurn(createQueuedTurnState(), {
      type: 'turn_started',
      turn_id: 'turn-early',
      data: { queued_id: 'queued-1', text: 'continue with the panel count' },
    })
    assert.equal(started.action, 'keep')
    const accepted = acceptQueuedTurn(started.state, queuedTurn)
    assert.equal(accepted.action, 'promote')
    assert.deepEqual(accepted.turn, { turnId: 'turn-early', text: queuedTurn.text })
    assert.equal(accepted.state.queuedTurn, null)
  })

  it('never promotes an identical-text turn without the queued_id', () => {
    const queued = acceptQueuedTurn(createQueuedTurnState(), queuedTurn)
    const unrelated = reconcileQueuedTurn(queued.state, {
      type: 'turn_started',
      turn_id: 'turn-other',
      data: { text: queuedTurn.text },
    })
    assert.equal(unrelated.action, 'keep')
    assert.deepEqual(unrelated.state, queued.state)
  })

  it('keeps the queue when a different queued_id starts with identical text', () => {
    const queued = acceptQueuedTurn(createQueuedTurnState(), queuedTurn)
    const unrelated = reconcileQueuedTurn(queued.state, {
      type: 'turn_started',
      turn_id: 'turn-other',
      data: { queued_id: 'queued-other', text: queuedTurn.text },
    })
    assert.equal(unrelated.action, 'keep')
    assert.deepEqual(unrelated.state.queuedTurn, queuedTurn)
  })

  it('clears only the matching dropped queue record', () => {
    const queued = acceptQueuedTurn(createQueuedTurnState(), queuedTurn)
    assert.equal(reconcileQueuedTurn(queued.state, {
      type: 'turn_queue_dropped',
      data: { queued_id: 'queued-1' },
    }).action, 'clear')
    assert.equal(reconcileQueuedTurn(queued.state, {
      type: 'turn_queue_dropped',
      data: { queued_id: 'queued-other' },
    }).action, 'keep')
  })

  it('bounds remembered queued starts to the most recent eight', () => {
    let state = createQueuedTurnState()
    for (let i = 0; i <= MAX_SEEN_QUEUED_STARTS; i += 1) {
      state = reconcileQueuedTurn(state, {
        type: 'turn_started',
        turn_id: `turn-${i}`,
        data: { queued_id: `queued-${i}`, text: `prompt ${i}` },
      }).state
    }
    assert.equal(state.seenQueuedStarts.length, MAX_SEEN_QUEUED_STARTS)
    assert.equal(state.seenQueuedStarts[0].queued_id, 'queued-1')
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

describe('queued-turn reconciliation: drop-before-202 (round 3)', () => {
  it('a drop that beats the 202 suppresses the note instead of stranding it', () => {
    let state = createQueuedTurnState()
    const dropped = reconcileQueuedTurn(state, {
      type: 'turn_queue_dropped', data: { queued_id: 'q-1', reason: 'entitlement_denied' },
    })
    assert.equal(dropped.action, 'keep')
    state = dropped.state
    const accepted = acceptQueuedTurn(state, { queuedId: 'q-1', text: 'late' })
    assert.equal(accepted.action, 'drop')
    assert.equal(accepted.state.queuedTurn, null)
  })

  it('an unrelated drop id does not suppress a later legitimate 202', () => {
    let state = createQueuedTurnState()
    state = reconcileQueuedTurn(state, {
      type: 'turn_queue_dropped', data: { queued_id: 'q-other' },
    }).state
    const accepted = acceptQueuedTurn(state, { queuedId: 'q-2', text: 'mine' })
    assert.equal(accepted.action, 'queue')
    assert.equal(accepted.state.queuedTurn.queuedId, 'q-2')
  })
})


// --- THE GUARD IS ON THE WIRE: source pins (slice 8a, round 3) -------------
//
// These are CENSUS and ORDERING pins, and they are the rows that make the PR's
// claim checkable at all. Three review rounds died on the same shape of
// defect: the guard installed per composer while the promise was per app, and
// the composer census short by one every time (the reply box, then the /try bar
// and the retry paths, then the Author-a-tool textarea). A behaviour test
// proves one path. These prove there is only one path to prove.
//
// EVERY assertion below reads COMMENT-STRIPPED source (readStripped, which uses
// esbuild's minifyWhitespace — a plain transform keeps comments, and a pin a
// comment can satisfy is not a pin) and compares with whitespace removed, so
// neither a claim in prose nor a reformat can move a row. Each was verified in
// both directions by neutering the property and watching it go red.
describe('the credential guard sits on the transport, not on the composers', () => {
  const GUARDED = [
    // [file, function opener, the first network call in its body]
    ['api.js', 'async function nlPrompt(', 'apiFetch('],
    ['api.js', 'async function authorTool(', 'apiFetch('],
    ['api.js', 'async function stageAuthorTool(', 'apiFetch('],
    ['converse.js', 'async function postMessage(', 'await post('],
    ['operatorClient.js', 'async function postMessage(', 'postJson('],
  ]

  for (const [file, opener, network] of GUARDED) {
    const label = opener.replace('async function ', '').replace('(', '')
    it(`${file} ${label} guards before it touches the network`, () => {
      const source = bare(readStripped(file))
      const at = source.indexOf(bare(opener))
      assert.notEqual(at, -1, `could not find ${opener} in ${file}`)
      const body = source.slice(at, at + 3000)
      const guardAt = body.indexOf('guardedText(')
      const throwAt = body.indexOf('newSecretRefusedError(')
      const networkAt = body.indexOf(bare(network))
      assert.notEqual(guardAt, -1, `${file} ${label} must call the guard seam`)
      assert.notEqual(throwAt, -1, `${file} ${label} must throw the typed refusal`)
      assert.notEqual(networkAt, -1, `could not find ${network} in ${file} ${label}`)
      assert.ok(guardAt < networkAt, `${file} ${label}: the guard must run before ${network}`)
      assert.ok(throwAt < networkAt, `${file} ${label}: the refusal must throw before ${network}`)
    })
  }

  // The endpoints that carry user-typed free text toward a model. Each may be
  // named by exactly ONE module that can issue a request, so a second sender
  // cannot appear beside the guarded one. (A mock or a doc comment naming an
  // endpoint is not a sender, and counting it would push this census toward
  // being switched off.)
  const ENDPOINTS = [
    ['/api/nl-prompt', 'api.js'],
    ['/api/author/stage', 'api.js'],
    ['/api/sessions/${encodeURIComponent(sessionId)}/messages', 'converse.js'],
    ['/api/operator/sessions/${encodeURIComponent(sessionId)}/messages', 'operatorClient.js'],
  ]

  it('every free-text endpoint is spoken by exactly one module', () => {
    for (const [endpoint, owner] of ENDPOINTS) {
      const senders = SENDERS.filter((file) => readStripped(file).includes(endpoint))
      assert.deepEqual(senders, [owner], `${endpoint} must be sent only by ${owner}`)
    }
  })

  // The composers, the controller and the shells must NOT evaluate the guard.
  // A local copy of the decision is what made three rounds of review read a
  // composer as the authority while some other path stayed open.
  it('only the seam evaluates the guard', () => {
    const evaluators = SRC_FILES.filter((file) => readStripped(file).includes('evaluateSecretGuard('))
    assert.deepEqual(evaluators, ['lib/secretGuardTransport.js', 'lib/secretPatterns.js'])
  })

  // guardedText has exactly one caller outside the transports: the author
  // pointer's STORAGE boundary, which writes the description to localStorage
  // before any transport runs. Named here so it stays a deliberate exception.
  it('the guard seam is called only by the transports and the storage boundary', () => {
    const callers = SRC_FILES.filter((file) => readStripped(file).includes('guardedText('))
    assert.deepEqual(callers, [
      'api.js',
      'controllers/useAuthorStageController.js',
      'converse.js',
      'lib/secretGuardTransport.js',
      'operatorClient.js',
    ])
  })

  // THE ROUND-3 FIX, pinned as an absence. Round 2's override was a latch: a
  // module variable in the controller and a ref in two components. Both hosts
  // short-circuit above the controller, so a "Send anyway" click whose
  // follow-on call never arrived left it armed, and the NEXT dispatch of ANY
  // text skipped the guard. The override is now a call parameter, so there is
  // nothing to strand.
  it('no override state is stored anywhere', () => {
    for (const file of SRC_FILES) {
      const source = readStripped(file)
      for (const latch of ['secretOverrideArmed', 'secretOverrideRef', 'allowSecretOnce()']) {
        assert.ok(!source.includes(latch), `${file} must not hold an override latch (${latch})`)
      }
    }
  })

  it('the override reaches the wire as a parameter on the call it authorises', () => {
    // The command bar and the /try bar hand it to dispatch; the controller
    // hands it to the router and the agent turn; the reply box and the author
    // panel hand it to their own transports.
    const carries = [
      ['components/PromptBox.jsx', 'dispatchPrompt(void 0, { allowSecretOnce: true })'],
      ['components/PromptBox.jsx', 'onDispatch(override, { images: attachments, allowSecretOnce })'],
      ['site/ToolCast.jsx', 'dispatchRequest(void 0, { allowSecretOnce: true })'],
      ['site/ToolCast.jsx', 'catalog.actions.dispatch(text, { allowSecretOnce })'],
      ['controllers/catalog/createCatalogController.js', 'services.routePrompt(current.mock, text, state.tools, { allowSecretOnce })'],
      ['controllers/catalog/createCatalogController.js', 'adapters.startAgentTurn(text, hint, { allowSecretOnce })'],
      ['components/ConversePanel.jsx', 'send(void 0, { allowSecretOnce: true })'],
      ['components/AuthorPanel.jsx', 'submit(void 0, { allowSecretOnce: true })'],
    ]
    for (const [file, snippet] of carries) {
      assert.ok(
        bare(readStripped(file)).includes(bare(snippet)),
        `${file} must carry the override as a call parameter: ${snippet}`,
      )
    }
  })

  it('the authorisation never rides the wire itself', () => {
    // converse.postMessage builds its payload field by field; allowSecretOnce
    // must be destructured out of the options and never assigned into it.
    const source = bare(readStripped('converse.js'))
    const at = source.indexOf('asyncfunctionpostMessage(')
    const body = source.slice(at, source.indexOf('await post(', at))
    assert.ok(body.includes('allowSecretOnce'), 'postMessage must accept the flag')
    assert.ok(!body.includes('payload.allowSecretOnce'), 'the flag must never enter the payload')
  })

  // The controller is the RENDER CHANNEL for the bar paths (App's Retry chip
  // and its R key dispatch around every composer), so it must catch the typed
  // refusal rather than let it read as a routing outage.
  it('the controller catches the refusal and publishes it, not a route error', () => {
    const source = bare(readStripped('controllers/catalog/createCatalogController.js'))
    const at = source.indexOf(bare('const dispatch = async (override,'))
    assert.notEqual(at, -1)
    const body = source.slice(at, at + 5000)
    assert.ok(body.includes('isSecretRefused(error)'), 'dispatch must recognise the refusal')
    const refusalAt = body.indexOf(bare('publish({ secretRefusal: error.refusal'))
    const routeErrAt = body.indexOf(bare('routeError: humanizeError(error)'))
    assert.notEqual(refusalAt, -1)
    assert.notEqual(routeErrAt, -1)
    assert.ok(refusalAt < routeErrAt, 'a refusal must be handled before the generic route error')
  })

  it('routePrompt still has exactly one caller, so no bar routes around it', () => {
    const source = readStripped('controllers/catalog/createCatalogController.js')
    assert.equal(source.split('services.routePrompt(').length - 1, 1)
  })

  it('the /try bar sends only through the controller, never straight to the router', () => {
    const source = readStripped('site/ToolCast.jsx')
    // nlPrompt may appear only as an import and as the routePrompt service.
    assert.equal(source.split('nlPrompt').length - 1, 2)
    assert.ok(!/nlPrompt\(/.test(source))
  })

  it('all four composers render a refusal with their own testids', () => {
    for (const [file, prefix] of [
      ['components/PromptBox.jsx', 'secret-notice'],
      ['components/ConversePanel.jsx', 'converse-secret-notice'],
      ['site/ToolCast.jsx', 'tc-secret-notice'],
      ['components/AuthorPanel.jsx', 'author-secret-notice'],
    ]) {
      const source = readStripped(file)
      assert.ok(source.includes(`"${prefix}"`), `${prefix} must be rendered`)
      assert.ok(source.includes(`"${prefix}-reason"`), `${prefix}-reason must be rendered`)
      assert.ok(source.includes(`"${prefix}-mask"`), `${prefix}-mask must be rendered`)
    }
  })

  it('both shells answer the mount question for the transports', () => {
    assert.ok(bare(readStripped('App.jsx')).includes('setCredentialMountAvailable(!mock)'))
    assert.ok(bare(readStripped('site/ToolCast.jsx')).includes('setCredentialMountAvailable(!transportMock)'))
  })
})

// PR #987 mechanical-fix round: two nits the security lens found once the
// override became a call parameter, both of them a refusal that got dropped
// on the floor rather than rendered or forwarded.
describe('a dropped refusal is still a leak (round 4)', () => {
  // ToolCast's PUBLIC_DEMO branch used to append `{ text, ... }` to the
  // visible demo transcript and clear the prompt UNCONDITIONALLY, including
  // when `dispatch` returned undefined because the transport refused — which
  // echoed the credential-shaped paste back onscreen and wiped the input
  // before "Send anyway" had anything left to re-issue. The fix reads the
  // controller's live state (not the render-time `secretRefusal`, which this
  // closure cannot trust after an `await`) and gates both the append and the
  // clear on it.
  it('a secret refusal never reaches the demo transcript, and the prompt survives it', () => {
    const source = bare(readStripped('site/ToolCast.jsx'))
    const refusedAt = source.indexOf('constrefused=!!catalog.controller.getState().secretRefusal')
    assert.notEqual(refusedAt, -1, 'dispatchRequest must read the live refusal off the controller')
    const guardAt = source.indexOf('if(PUBLIC_DEMO&&!refused){', refusedAt)
    assert.notEqual(guardAt, -1, 'the demo-transcript branch must be gated on !refused')
    assert.ok(guardAt > refusedAt, 'refused must be computed before it gates the branch')
    const actionableAt = source.indexOf('constactionable=', guardAt)
    assert.notEqual(actionableAt, -1)
    const appendAt = source.indexOf(
      'setDemoTurns(current=>[...current,{id,text,reply:demoReplyFor(text,decision)}]);',
      guardAt,
    )
    const clearAt = source.indexOf('setPrompt("");', guardAt)
    assert.notEqual(appendAt, -1, 'the transcript append must still exist on the real-dispatch path')
    assert.notEqual(clearAt, -1, 'the prompt clear must still exist on the real-dispatch path')
    assert.ok(appendAt > guardAt && appendAt < actionableAt, 'the append must sit inside the !refused branch')
    assert.ok(clearAt > guardAt && clearAt < actionableAt, 'the prompt clear must sit inside the !refused branch')
  })

  // useAuthorStageController minted the AuthorPanel's turn authority with
  // `authorityProvider(initial.description)` — no allowSecretOnce — so a
  // "Send anyway" re-stage of credential-shaped text had the AUTHORITY MINT
  // itself refused (it starts a converse turn on the same guarded transport)
  // and silently fell back to null-authority. The override the user just
  // clicked never reached the call it was meant to authorise.
  it('the authority mint forwards allowSecretOnce, both shells', () => {
    const authSource = bare(readStripped('controllers/useAuthorStageController.js'))
    assert.ok(
      authSource.includes('authorityProvider(initial.description,{allowSecretOnce})'),
      'useAuthorStageController must forward allowSecretOnce into the authority mint',
    )
    for (const file of ['App.jsx', 'site/ToolCast.jsx']) {
      const source = bare(readStripped(file))
      assert.ok(
        /authorAuthorityProvider=useCallback\(async\(description,\{allowSecretOnce=false\}=\{\}\)=>/.test(source),
        `${file}: authorAuthorityProvider must accept allowSecretOnce`,
      )
      assert.ok(
        /,\{allowSecretOnce\}\)/.test(source),
        `${file}: authorAuthorityProvider must forward allowSecretOnce to its turn-start call`,
      )
    }
  })
})
