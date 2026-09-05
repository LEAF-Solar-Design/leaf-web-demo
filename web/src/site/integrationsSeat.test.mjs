// SurfaceFrame.Integrations, pinned at the source.
//
// Standardization slice 8c. The Link-a-service drawer follows the exact
// #1029 lesson conversationListSeat.test.mjs already pins for
// SurfaceFrame.Conversations: a rail-column mount that shields nothing in
// the drawing window, never a mount inside the overlay-card region (the
// decision-card / converse-panel flow) where a painted popover head could
// eat pan, zoom and select clicks meant for the ground canvas.
//
// This reads App.jsx and ToolCast.jsx as text, the same style app-wiring
// and conversationListSeat pin wiring facts, so it goes red if someone
// moves the mount back into a card region or drops the rail column it
// depends on.
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

const app = readFileSync(new URL('../App.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const toolCast = readFileSync(new URL('./ToolCast.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')

const MOUNT = '<SurfaceFrame.Integrations />'

describe('the console Link-a-service drawer sits in the rail column, not over the ground', () => {
  it('mounts it exactly once in App.jsx', () => {
    assert.equal(app.split(MOUNT).length - 1, 1, `${MOUNT} must appear exactly once in App.jsx`)
  })

  it('mounts it inside the .rail-stack column, after the job monitor and inbox', () => {
    const stackOpen = app.indexOf('<div className="rail-stack">')
    assert.notEqual(stackOpen, -1, 'App.jsx must open a <div className="rail-stack"> column')
    const jobRail = app.indexOf('<SurfaceFrame.JobRail />', stackOpen)
    const inbox = app.indexOf('<SurfaceFrame.Inbox />', jobRail)
    const mount = app.indexOf(MOUNT, inbox)
    const stackClose = app.indexOf('</div>', mount)
    assert.ok(stackOpen < jobRail && jobRail < inbox && inbox < mount && mount < stackClose,
      'the drawer must be mounted after the job monitor and inbox, inside the .rail-stack column')
  })

  it('does not mount it in the overlay-card region between the decision cards and the ConversePanel', () => {
    const decisionCard = app.indexOf('<AnnotationDecisionCard')
    const conversePanel = app.indexOf('<ConversePanel', decisionCard)
    assert.ok(decisionCard !== -1 && conversePanel !== -1, 'both card-region anchors must exist')
    const between = app.slice(decisionCard, conversePanel)
    assert.equal(between.includes(MOUNT), false,
      'the drawer must not sit in the overlay-card region, where it shields the drawing window')
  })
})

describe('the stage Link-a-service drawer sits in aside.tc-rail-r, not the operator card region', () => {
  it('mounts it exactly once in ToolCast.jsx', () => {
    assert.equal(toolCast.split(MOUNT).length - 1, 1, `${MOUNT} must appear exactly once in ToolCast.jsx`)
  })

  it('mounts it inside the aside.tc-rail-r rail column, beside the account trigger', () => {
    const railOpen = toolCast.indexOf('tc-rail tc-rail-r')
    assert.notEqual(railOpen, -1, 'ToolCast.jsx must open the aside.tc-rail-r column')
    const mount = toolCast.indexOf(MOUNT, railOpen)
    const railClose = toolCast.indexOf('</aside>', mount)
    assert.ok(railOpen < mount && mount < railClose,
      'the drawer must be mounted inside aside.tc-rail-r')
  })

  it('does not mount it in the operator card region between Conversations and the ConversePanel', () => {
    const conversations = toolCast.indexOf('<SurfaceFrame.Conversations />')
    const conversePanel = toolCast.indexOf('<ConversePanel', conversations)
    assert.ok(conversations !== -1 && conversePanel !== -1, 'both card-region anchors must exist')
    const between = toolCast.slice(conversations, conversePanel)
    assert.equal(between.includes(MOUNT), false,
      'the drawer must not sit in the operator tabpanel card region')
  })
})
