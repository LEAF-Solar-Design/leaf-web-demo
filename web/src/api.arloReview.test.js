import { afterEach, describe, expect, it, vi } from 'vitest'
import { getArloReviews, recordToEnvelope, saveArloReview } from './api.js'

const context = { org_id:'11111111-1111-4111-8111-111111111111', project_id:'22222222-2222-4222-8222-222222222222', job_id:'33333333-3333-4333-8333-333333333333', input_version_id:'44444444-4444-4444-8444-444444444444' }
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('ARLO canonical result and review transport', () => {
  it('adapts a canonical solve without changing its data or inventing CAD application', () => {
    const result = { solver:'arlo-design', solver_result:{ proposals:[] }, source_sha256:'a'.repeat(64) }
    const record = { ...context, status:'complete', result }
    const envelope = recordToEnvelope(record)
    expect(envelope.ok).toBe(true)
    expect(envelope.result).toBe(result)
    expect(envelope.job_context).toEqual(context)
    expect(envelope.overlay).toBeNull()
    expect(envelope.cost).toBeNull()
    const ordinary = { ok:true, result:{ counts:{ wall:2 } } }
    expect(recordToEnvelope({ status:'complete', result:ordinary })).toBe(ordinary)
  })

  it('uses the authenticated canonical project endpoint and exact decision payload', async () => {
    localStorage.setItem('leaf.jwt','test-session')
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ reviews:[] }), { status:200, headers:{'Content-Type':'application/json'} }))
    vi.stubGlobal('fetch',fetch)
    await getArloReviews(context)
    const body = { proposal_id:'proposal-a', result_sha256:'a'.repeat(64), decision:'accept', note:'Checked' }
    await saveArloReview(context,body,'review-key')
    expect(fetch.mock.calls[0][0]).toContain(`/api/projects/${context.project_id}/arlo-jobs/${context.job_id}/reviews`)
    const request = fetch.mock.calls[1][1]
    expect(request.method).toBe('POST')
    expect(request.headers.Authorization).toBe('Bearer test-session')
    expect(request.headers['X-Org-Id']).toBe(context.org_id)
    expect(request.headers['Idempotency-Key']).toBe('review-key')
    expect(JSON.parse(request.body)).toEqual(body)
  })

  it('propagates a rejected save instead of treating it as a saved decision', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail:'Stale proposal' }),{status:409,headers:{'Content-Type':'application/json'}})))
    await expect(saveArloReview(context,{},'key')).rejects.toThrow()
  })
})
