import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  accountManifestGraph,
  percentile,
} from './measure_cad_edit_closeout.mjs'

test('percentile uses nearest rank without changing samples', () => {
  const samples = [40, 10, 30, 20, 100, 90, 80, 70, 60, 50]
  assert.equal(percentile(samples, 50), 50)
  assert.equal(percentile(samples, 95), 100)
  assert.deepEqual(samples, [40, 10, 30, 20, 100, 90, 80, 70, 60, 50])
})

test('percentile rejects empty, non-finite, and out-of-range input', () => {
  assert.throws(() => percentile([], 95), /at least one finite sample/)
  assert.throws(() => percentile([1, Number.NaN], 95), /finite numbers/)
  assert.throws(() => percentile([1], 0), /greater than 0/)
})

test('manifest accounting follows static imports and CSS only', () => {
  const manifest = {
    'index.html': {
      file: 'assets/entry.js',
      isEntry: true,
      imports: ['_shared.js'],
      dynamicImports: ['src/later.js'],
      css: ['assets/main.css'],
    },
    '_shared.js': {
      file: 'assets/shared.js',
      css: ['assets/shared.css', 'assets/main.css'],
    },
    'src/later.js': {
      file: 'assets/later.js',
      css: ['assets/later.css'],
    },
  }
  const assets = new Map([
    ['assets/entry.js', Buffer.from('entry')],
    ['assets/shared.js', Buffer.from('shared')],
    ['assets/main.css', Buffer.from('main')],
    ['assets/shared.css', Buffer.from('shared-css')],
  ])
  const result = accountManifestGraph(manifest, 'index.html', assets)
  assert.deepEqual(result.files.map(({ file }) => file), [
    'assets/entry.js',
    'assets/main.css',
    'assets/shared.css',
    'assets/shared.js',
  ])
  assert.equal(result.files.some(({ file }) => file.includes('later')), false)
  assert.equal(result.gzip_bytes, result.files.reduce((sum, file) => sum + file.gzip_bytes, 0))
  assert.match(result.files[0].sha256, /^[a-f0-9]{64}$/)
})

test('manifest accounting reports missing static artifacts', () => {
  assert.throws(
    () => accountManifestGraph({ entry: { file: 'missing.js' } }, 'entry', new Map()),
    /initial artifact not found: missing.js/,
  )
})
