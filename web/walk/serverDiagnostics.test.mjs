import test from 'node:test'
import assert from 'node:assert/strict'
import { extractErrorId, serverDiagnostic } from './serverDiagnostics.mjs'

test('extractErrorId reads the response body and the server log forms', () => {
  assert.equal(extractErrorId('{"error":{"code":"internal","message":"internal server error (error_id: 0123456789abcdef)","retryable":false}}'), '0123456789abcdef')
  assert.equal(extractErrorId('unhandled exception [error_id=fedcba9876543210] serving GET /api/capabilities: boom'), 'fedcba9876543210')
  for (const text of ['error_id: XYZ', 'error_id: 0123', '', null, undefined, 42]) {
    assert.equal(extractErrorId(text), null)
  }
})

test('serverDiagnostic slices from the log line that names the error id', () => {
  const output = 'noise line\nINFO: 127.0.0.1 GET /x 200\nERROR:envelopes:unhandled exception [error_id=0123456789abcdef] serving GET /api/capabilities: AttributeError\nTraceback (most recent call last):\n  File "x.py", line 1\nAttributeError: boom\nINFO: after\n'
  const result = serverDiagnostic(output, '0123456789abcdef')
  assert.equal(result.matched, true)
  assert.ok(result.text.startsWith('ERROR:envelopes:unhandled exception [error_id=0123456789abcdef]'))
  assert.ok(result.text.includes('AttributeError: boom'))
  const short = serverDiagnostic(output, '0123456789abcdef', 40)
  assert.equal(short.matched, true)
  assert.equal(short.text.length, 40)
  assert.equal(short.text, result.text.slice(0, 40))
  assert.equal(serverDiagnostic(`${output}${output}`, '0123456789abcdef').text, result.text)
})

test('serverDiagnostic falls back to the buffer tail when the id is absent', () => {
  const output = 'noise line\nerror_id: 0123456789abcdef\nINFO: after\n'
  for (const errorId of ['fedcba9876543210', null, '0123456789abcdef']) {
    assert.deepEqual(serverDiagnostic(output, errorId, 10), { matched: false, text: output.slice(-10) })
  }
})

test('serverDiagnostic tolerates a missing buffer', () => {
  for (const output of [undefined, null, 7]) {
    assert.deepEqual(serverDiagnostic(output, '0123456789abcdef'), { matched: false, text: '' })
  }
})
