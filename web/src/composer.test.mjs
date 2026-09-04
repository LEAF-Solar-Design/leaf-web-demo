import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
const conversePanelStripped = strip('./components/ConversePanel.jsx')
const toolCastStripped = strip('./site/ToolCast.jsx')
const catalogControllerStripped = strip('./controllers/catalog/createCatalogController.js')

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


// --- the credential guard's shape, read off the source (slice 8a round 2) ---
//
// These are ORDERING and CENSUS pins, which is exactly what unit tests over
// pure modules and jsdom tests over one component each could not give. Two
// review rounds died on the same defect: the guard installed per composer while
// the promise was per app. A behaviour test proves one path; these prove there
// is only one path to prove.
//
// esbuild strips comments, so a pin cannot be satisfied by a comment claiming
// the property. Verified in both directions by neutering each property and
// watching the matching row go red.
describe('credential guard: the funnel is the authority', () => {
  const orderIn = (source, opener, first, second, window = 2000) => {
    const start = source.indexOf(opener)
    assert.notEqual(start, -1, `could not find ${opener}`)
    const body = source.slice(start, start + window)
    const a = body.indexOf(first)
    const b = body.indexOf(second)
    assert.notEqual(a, -1, `could not find ${first}`)
    assert.notEqual(b, -1, `could not find ${second}`)
    return a < b
  }

  it('the controller evaluates the guard before the router ever sees the text', () => {
    const dispatchAt = catalogControllerStripped.indexOf('const dispatch = async (override)')
    assert.notEqual(dispatchAt, -1)
    const body = catalogControllerStripped.slice(dispatchAt, dispatchAt + 4000)
    const guardAt = body.indexOf('evaluateSecretGuard(')
    const routeAt = body.indexOf('services.routePrompt(')
    assert.notEqual(guardAt, -1, 'dispatch must evaluate the guard')
    assert.notEqual(routeAt, -1, 'dispatch must be the one caller of routePrompt')
    assert.ok(guardAt < routeAt, 'the guard must run before routePrompt')
  })

  it('the controller reads-and-disarms the override above every early return', () => {
    // The round-1 fail-open, ported: an override read BELOW the busy/empty
    // return latches armed when the dispatch is a no-op, and the next
    // unrelated Enter spends it on a hard-refusal shape.
    assert.ok(
      orderIn(
        catalogControllerStripped,
        'const dispatch = async (override)',
        'secretOverrideArmed = false',
        'current.running) return',
      ),
      'the override must be spent before dispatch can return early',
    )
  })

  it('routePrompt has exactly one caller, so no bar can route around the guard', () => {
    const callers = catalogControllerStripped.split('services.routePrompt(').length - 1
    assert.equal(callers, 1)
  })

  it('the reply box reads-and-disarms above its busy early return', () => {
    assert.ok(
      orderIn(
        conversePanelStripped,
        'const send = async (nextText = input)',
        'secretOverrideRef.current = false',
        '|| busy) return false',
      ),
      'ConversePanel.send must spend the override before it can return early',
    )
  })

  it('the reply box guards before postMessage', () => {
    assert.ok(
      orderIn(
        conversePanelStripped,
        'const send = async (nextText = input)',
        'evaluateSecretGuard(',
        'postMessage(sessionId',
        4000,
      ),
    )
  })

  // /try's ToolCast bar carries the SAME data-testid and aria-label as the app
  // bar, and it is the composer both earlier review rounds missed. It has no
  // guard of its own BY DESIGN: everything it sends goes through the funnel,
  // and it renders the funnel's verdict.
  it('the /try bar sends only through the controller, never straight to the router', () => {
    assert.match(toolCastStripped, /catalog\.actions\.dispatch\(text\)/)
    // nlPrompt is wired in as the controller's routePrompt service and is never
    // called directly; a direct call would be a bar around the funnel.
    const direct = toolCastStripped.split('nlPrompt').length - 1
    assert.equal(direct, 2, 'nlPrompt may appear only as an import and as the routePrompt service')
    assert.doesNotMatch(toolCastStripped, /nlPrompt\(/)
  })

  it('the /try bar renders the funnel refusal with its own testids', () => {
    assert.match(toolCastStripped, /"tc-secret-notice"/)
    assert.match(toolCastStripped, /"tc-secret-notice-reason"/)
    assert.match(toolCastStripped, /"tc-secret-notice-mask"/)
    assert.match(toolCastStripped, /secretRefusal\.reason/)
    assert.match(toolCastStripped, /secretRefusal\.masked/)
    // The override is a controller action, so it is spent by the controller.
    assert.match(toolCastStripped, /catalog\.actions\.allowSecretOnce\(\)/)
  })

  it('both bars answer the mount question, so the copy is honest per mode', () => {
    assert.match(toolCastStripped, /credentialMountAvailable:\s*!transportMock/)
    assert.match(promptBoxStripped, /evaluateSecretGuard\(sent,\s*\{\s*credentialMountAvailable\s*\}\)/)
  })

  it('the app bar renders the controller verdict, not only its own', () => {
    assert.match(promptBoxStripped, /const shownSecret = secretRefusal \|\| secretNotice/)
    assert.match(promptBoxStripped, /onAllowSecretOnce\?\.\(\)/)
  })
})
