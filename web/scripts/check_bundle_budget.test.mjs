import assert from 'node:assert/strict'
import { test } from 'node:test'
import { beforeReference, isolatedChunks, stemFailures } from './check_bundle_budget.mjs'

test('before overlay uses the merge-base or an explicit ref, with no HEAD fallback', () => {
  assert.equal(beforeReference([], 'merge-base-sha'), 'merge-base-sha')
  assert.equal(beforeReference(['--before', 'original-canvas-parent'], 'merge-base-sha'), 'original-canvas-parent')
  assert.throws(() => beforeReference([], null), /merge-base.*--before/)
  assert.throws(() => beforeReference(['--before'], 'merge-base-sha'), /requires a git ref/)
})

test('identical before and after bundles cannot reseed, even with chunk order changed', () => {
  assert.throws(() => isolatedChunks({ chunks: {} },
    { chunks: { 'index.js': 12, 'index.css': 3 } },
    { chunks: { 'index.css': 3, 'index.js': 12 } }), /identical.*refusing/)
})

test('isolates only the canvas delta and requires both lazy stems when reseeding', () => {
  const result = isolatedChunks({ chunks: { 'index.js': 10 }, required_stems: ['index.js'] },
    { chunks: { 'index.js': 20 } },
    { chunks: { 'index.js': 20, 'LabCanvas.js': 100, 'LabCanvas.css': 5 } })
  assert.deepEqual(result.chunks, { 'index.js': 10, 'LabCanvas.js': 100, 'LabCanvas.css': 5 })
  assert.deepEqual(result.required_stems, ['index.js', 'LabCanvas.js', 'LabCanvas.css'])
})

test('every capped stem must be present even without required_stems', () => {
  const baseline = { stem_caps: { 'LabCanvas.js': 100, 'LabCanvas.css': 10 } }
  assert.match(stemFailures({}, baseline).join('\n'), /LabCanvas.js missing/)
  assert.match(stemFailures({ 'LabCanvas.js': 90 }, baseline).join('\n'), /LabCanvas.css missing/)
  assert.deepEqual(stemFailures({ 'LabCanvas.js': 100, 'LabCanvas.css': 10 }, baseline), [])
  assert.match(stemFailures({ 'LabCanvas.js': 101, 'LabCanvas.css': 10 }, baseline).join('\n'), /hard stem cap/)
})
