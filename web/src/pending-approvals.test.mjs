import assert from 'node:assert/strict'
import { afterEach, describe, it } from 'node:test'

import { approveStandardService, listPendingApprovals, resolveApproval } from './converse.js'

const originalFetch = globalThis.fetch

afterEach(() => { globalThis.fetch = originalFetch })

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

describe('pending approvals', () => {
  it('executes a standard service only through the authenticated human route', async () => {
    let request
    globalThis.fetch = async (url, options) => {
      request = { url, options }
      return response(200, { status: 'completed', receipt_id: 'b'.repeat(64) })
    }

    const receipt = await approveStandardService('approval_12345678', 'a'.repeat(64))

    assert.equal(receipt.status, 'completed')
    assert.equal(request.url, '/api/mcp/gateway/approvals/execute')
    assert.equal(request.options.method, 'POST')
    assert.deepEqual(JSON.parse(request.options.body), {
      approval_id: 'approval_12345678',
      argument_digest: 'a'.repeat(64),
    })
  })

  it('loads the bounded tenant inbox', async () => {
    let request
    globalThis.fetch = async (url, options) => {
      request = { url, options }
      return response(200, { approvals: [{ confirmation_id: 'confirm-1' }] })
    }
    assert.deepEqual(await listPendingApprovals('session one', 500), [{ confirmation_id: 'confirm-1' }])
    assert.match(request.url, /\/api\/agent\/approvals\/pending\?session_id=session%20one&limit=100$/)
    assert.equal(request.options.method, 'GET')
  })

  it('records and resumes through the approval owning session', async () => {
    const requests = []
    globalThis.fetch = async (url, options) => {
      requests.push({ url, options })
      return requests.length === 1
        ? response(200, { resolved: true, approved: true })
        : response(202, { turn_id: 'resume-turn', status: 'started' })
    }
    const result = await resolveApproval('confirm-1', 'owning-session', true)
    assert.equal(result.turn_id, 'resume-turn')
    assert.match(requests[0].url, /\/api\/agent\/approvals\/confirm-1$/)
    assert.match(requests[1].url, /\/api\/sessions\/owning-session\/messages$/)
    assert.deepEqual(JSON.parse(requests[1].options.body), {
      confirm: { confirmationId: 'confirm-1', approved: true },
    })
  })

  it('resumes a recorded decision without trying to decide it again', async () => {
    const requests = []
    globalThis.fetch = async (url, options) => {
      requests.push({ url, options })
      return response(202, { turn_id: 'resume-turn', status: 'started' })
    }
    await resolveApproval('confirm-2', 'owning-session', false, true)
    assert.equal(requests.length, 1)
    assert.match(requests[0].url, /\/api\/sessions\/owning-session\/messages$/)
    assert.deepEqual(JSON.parse(requests[0].options.body), {
      confirm: { confirmationId: 'confirm-2', approved: false },
    })
  })

  it('never resumes after a conflicting stale decision', async () => {
    const requests = []
    globalThis.fetch = async (url, options) => {
      requests.push({ url, options })
      return response(409, {
        error: { error_code: 'BAD_PARAMS', message: 'already decided' },
      })
    }
    await assert.rejects(
      resolveApproval('confirm-3', 'owning-session', false),
      (error) => error.status === 409,
    )
    assert.equal(requests.length, 1)
    assert.match(requests[0].url, /\/api\/agent\/approvals\/confirm-3$/)
  })
})
