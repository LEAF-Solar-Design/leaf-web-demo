// Guard: a 413 must reach the user as "too large", not as an outage.
//
// The app answers an oversized turn with `413 BAD_PARAMS`. classifyAgentError
// recognised BAD_PARAMS only at status 409 (the approval-stale case), so a 413
// fell through to 'unreachable' and the panel said "Couldn't reach the
// assistant" about a request the assistant had answered perfectly clearly.
// That is the difference between a message the user can act on (send fewer or
// smaller images) and one that sends them looking for an outage.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import { classifyAgentError } from './converse.js'

describe('classifyAgentError', () => {
  it('classifies a 413 as too_large', () => {
    assert.equal(classifyAgentError({ status: 413, errorCode: 'BAD_PARAMS' }), 'too_large')
    // ...including when the body carries no machine code at all.
    assert.equal(classifyAgentError({ status: 413 }), 'too_large')
  })

  it('does not disturb the other BAD_PARAMS cases', () => {
    assert.equal(classifyAgentError({ status: 409, errorCode: 'BAD_PARAMS' }), 'approval_stale')
    assert.equal(classifyAgentError({ status: 410, errorCode: 'BAD_PARAMS' }), 'confirmation_expired')
    assert.equal(classifyAgentError({ status: 409, errorCode: 'turn_in_progress' }), 'busy')
    assert.equal(classifyAgentError({ status: 401 }), 'grant')
    assert.equal(classifyAgentError({ status: 403 }), 'entitlement')
    assert.equal(classifyAgentError({ status: 502 }), 'unreachable')
  })

  it('gives the too_large kind its own panel message', () => {
    // The kind is only useful if the panel says something different for it.
    const source = readFileSync(
      new URL('./components/ConversePanel.jsx', import.meta.url), 'utf8')
    const line = source.split('\n').find((l) => l.includes("kind === 'too_large'"))
    assert.ok(line, 'ConversePanel has no branch for too_large')
    assert.ok(!/Couldn.t reach the assistant/.test(line),
      'too_large must not reuse the outage copy')
  })
})
