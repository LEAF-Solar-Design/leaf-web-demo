import { expect, it } from 'vitest'
import { humanizeError, MSG_GENERIC, MSG_NETWORK, MSG_SERVICE } from './errorHumanize.js'

it('maps Safari Load failed to the network message', () => {
  expect(humanizeError(new Error('Load failed'))).toBe(MSG_NETWORK)
})

it('preserves Upload failed as a readable error', () => {
  expect(humanizeError(new Error('Upload failed'))).toBe('Upload failed')
})

it('preserves Download failed as a readable error', () => {
  expect(humanizeError(new Error('Download failed'))).toBe('Download failed')
})

it('SSD1-B2 row1', () => {
  const value = JSON.parse('{"toString":0}')
  expect(() => humanizeError(value)).not.toThrow()
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row2', () => {
  const value = {
    get message() {
      throw new Error('Cannot read message')
    },
  }
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row3', () => {
  const value = {
    get name() {
      throw new Error('Cannot read name')
    },
  }
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row4', () => {
  expect(humanizeError(null)).toBe(MSG_GENERIC)
  expect(humanizeError(new Error('Could not load the drawing.'))).toBe('Could not load the drawing.')
  expect(humanizeError(new TypeError('Failed to fetch'))).toBe(MSG_NETWORK)
  expect(humanizeError('POST /api/run -> 502')).toBe(MSG_SERVICE)
})

it('preserves structured refusals on the normal thrown API error path', () => {
  const error = new Error('POST /api/run -> 502')
  error.body = { error: { reason_code: 'SOLAREDGE_GRID_AMBIGUOUS', error_code: 'BAD_PARAMS',
    frame_ref: 'roof:A', candidate_ids: [2, 9], action: 'pick_candidate' } }
  expect(humanizeError(error)).toBe('Group roof:A: multiple SolarEdge grids (candidates 2, 9). Select one grid.')
  error.body = { error: { reason_code: 'SOLVE_TIMEOUT', message: 'Solve Roof-A timed out.', retryable: true } }
  expect(humanizeError(error)).toBe('Solve Roof-A timed out. Reconcile the solve status before retrying.')
})

it('leaves legacy transport strings and unrelated product messages stable', () => {
  expect(humanizeError('POST /api/run -> 502')).toBe(MSG_SERVICE)
  expect(humanizeError('Failed to fetch')).toBe(MSG_NETWORK)
  for (const message of ['Daily quota reached.', 'Tool authoring is disabled for this workspace.']) {
    expect(humanizeError({ error_code: 'AUTHORING_DISABLED', message })).toBe(message)
  }
})
