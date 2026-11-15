// Portable source diagnostic units, no server, browser, credentials or lease.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readSourceFailureBody, sourceInitializationError, sourceInitializationMessage } from '../../web/src/lib/sourceFailureDiagnostic.js'

const request = '11111111-1111-4111-8111-111111111111'
const correlation = '22222222-2222-4222-8222-222222222222'
const message = 'Project source initialization failed. Review the original request before retrying.'
const body = () => ({ error: { code: 'author_unavailable', message, source_request_id: request,
  correlation_id: correlation, phase: 'stage', author_subphase: 'initialize_http', author_http_status: 503 } })
const response = (raw, status = 503) => new Response(raw, { status, headers: { 'Content-Type': 'application/json' } })

test('bounded actual HTTP body produces request-bound fixed text', async () => {
  const parsed = await readSourceFailureBody(response(JSON.stringify(body())))
  const error = sourceInitializationError(parsed, request)
  assert.equal(error.status, 503)
  assert.match(sourceInitializationMessage(error, request), new RegExp(correlation))
  assert.equal(sourceInitializationMessage(error, '33333333-3333-4333-8333-333333333333'), '')
})

test('malformed or foreign diagnostics cannot replace generic error', () => {
  for (const change of [{ source_request_id: '33333333-3333-4333-8333-333333333333' },
    { correlation_id: 'PRIVATE' }, { phase: 'publish' }, { author_subphase: 'PRIVATE' },
    { author_http_status: true }, { author_http_status: 999 }, { message: 'PRIVATE' }, { secret: 'PRIVATE' }]) {
    const value = body(); Object.assign(value.error, change)
    assert.equal(sourceInitializationError(value, request), null)
  }
  assert.equal(sourceInitializationMessage(Object.assign(new Error('PRIVATE'), body().error), request), '')
})

test('all closed initializer phases accepted without optional HTTP status', () => {
  for (const author_subphase of ['initialize_binding', 'initialize_authority', 'initialize_http',
    'initialize_transport', 'initialize_response', 'initialization']) {
    const value = body(); value.error.author_subphase = author_subphase; delete value.error.author_http_status
    assert.ok(sourceInitializationError(value, request))
  }
})

test('bounded reader rejects oversized JSON, invalid encoding and wrong status', async () => {
  assert.equal(await readSourceFailureBody(response(JSON.stringify({ text: 'x'.repeat(4097) }))), null)
  assert.equal(await readSourceFailureBody(response(new Uint8Array([255]))), null)
  assert.equal(await readSourceFailureBody(response('{}', 500)), null)
})
