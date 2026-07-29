// Guard: the transcript fold must SKIP queue bookkeeping events.
//
// `turn_queued` / `turn_queue_dropped` carry no turn_id; ConversePanel's fold
// calls `turnOf(env.turn_id)` for every event it does not explicitly skip, and
// a null id mints a fresh synthetic `_N` turn — a blank bubble per queue event
// (PR #301 review round 1, finding 5). The fold lives in JSX (not importable
// under node --test), so this asserts the skip at the source level through the
// same esbuild comment-stripping lens app-wiring.test.mjs uses: the skip must
// exist in EXECUTABLE code, before the turnOf call, for exactly these types.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import esbuild from 'esbuild'

const source = readFileSync(
  new URL('./components/ConversePanel.jsx', import.meta.url), 'utf8')
const stripped = esbuild.transformSync(source, { loader: 'jsx' }).code

describe('ConversePanel transcript fold', () => {
  for (const type of ['turn_queued', 'turn_queue_dropped']) {
    it(`skips ${type} before minting a turn`, () => {
      const skipAt = stripped.indexOf(`"${type}"`)
      assert.ok(skipAt !== -1,
        `${type} is never mentioned in executable code — queue events will ` +
        'mint empty synthetic turns')
      const foldAt = stripped.indexOf('turnOf(env.turn_id)')
      assert.ok(foldAt !== -1, 'the fold call itself was not found — update this guard')
      assert.ok(skipAt < foldAt,
        `${type} is only referenced AFTER the fold call — the skip does not ` +
        'protect turnOf')
    })
  }
})
