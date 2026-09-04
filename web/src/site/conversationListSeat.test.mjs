// The console's ConversationList seat, pinned at the source.
//
// Slice 6b first mounted <SurfaceFrame.Conversations /> inside <main>, in the
// same normal-flow subtree as the workspace card, between the decision cards
// and the ConversePanel. On a fresh /app boot with RAIL=1 its painted head sat
// over the centre of .viewer-wrap and ate pan, zoom and select: the local e2e
// row "the pointer chain punches through the card window to the ground canvas"
// (e2e/local/one-shell-mount.spec.mjs) reported elementFromPoint hitting
// DIV.conversation-list-head outside the ground. The fix moved the mount into
// the right rail column (.rail-stack, which takes over aside.rail's grid seat)
// beside the job monitor.
//
// This pin reads App.jsx and styles.css as text, the way app-wiring.test.mjs
// pins wiring facts, so it goes red if someone moves the mount back into the
// card region or drops the grid seat the column depends on. It is the
// deterministic half of the proof; the e2e row above is the layout half.
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

const app = readFileSync(new URL('../App.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const css = readFileSync(new URL('../styles.css', import.meta.url), 'utf8').replace(/\r\n/g, '\n')

const MOUNT = '<SurfaceFrame.Conversations />'

describe('the console ConversationList sits in the rail column, not over the ground', () => {
  it('mounts the list exactly once in App.jsx', () => {
    assert.equal(app.split(MOUNT).length - 1, 1, `${MOUNT} must appear exactly once`)
  })

  it('mounts it inside the .rail-stack column beside the job monitor', () => {
    const stackOpen = app.indexOf('<div className="rail-stack">')
    assert.notEqual(stackOpen, -1, 'App.jsx must open a <div className="rail-stack"> column')
    const mount = app.indexOf(MOUNT)
    const jobRail = app.indexOf('<SurfaceFrame.JobRail />', stackOpen)
    const stackClose = app.indexOf('</div>', jobRail)
    assert.ok(stackOpen < mount && mount < jobRail && jobRail < stackClose,
      'the list must be mounted between the .rail-stack opening tag and the job monitor inside it')
  })

  it('does not mount it in the card region between the decision cards and the ConversePanel', () => {
    const decisionCard = app.indexOf('<AnnotationDecisionCard')
    const conversePanel = app.indexOf('<ConversePanel', decisionCard)
    assert.ok(decisionCard !== -1 && conversePanel !== -1, 'both card-region anchors must exist')
    const between = app.slice(decisionCard, conversePanel)
    assert.equal(between.includes(MOUNT), false,
      'the list must not sit in the overlay-card region, where it shields the drawing window')
  })

  it('the column owns the rail grid seat on desktop and the stacked order on narrow layouts', () => {
    assert.match(css, /@media \(min-width: 981px\) \{\s*\.rail-stack \{ grid-row: 2; grid-column: 3; \}/,
      '.rail-stack must claim grid-row 2 / grid-column 3 on desktop, the seat aside.rail held alone')
    assert.match(css, /\.app > \.rail-stack \{ order: 3; \}/,
      '.rail-stack must keep the rail\'s order 3 in the stacked (<=980px) layout')
  })

  it('bounds the list inside the column so it can never push the job monitor off the rail', () => {
    assert.match(css, /\.rail-stack \.conversation-list \{[^}]*max-height: 40%;[^}]*overflow-y: auto;/s,
      'the list must be capped at 40% of the column and scroll independently')
  })
})
