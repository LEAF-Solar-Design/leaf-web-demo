import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { buildFingerprint, distDigest, recordPrebuilt, reusablePrebuilt, RECEIPT_SCHEMA } from './ci-prebuilt.mjs'

function fixture(t) {
  const webRoot = mkdtempSync(path.join(os.tmpdir(), 'leaf-ci-prebuilt-'))
  t.after(() => rmSync(webRoot, { recursive: true, force: true }))
  const dist = path.join(webRoot, 'dist')
  mkdirSync(path.join(dist, '_next/static'), { recursive: true })
  writeFileSync(path.join(dist, 'index.html'), '<html>fixture</html>')
  writeFileSync(path.join(dist, '_next/static/a.js'), 'console.log("fixture")')
  writeFileSync(path.join(dist, 'web-build-manifest.json'), JSON.stringify({ source_sha: 'source-a' }))
  const options = { webRoot, receiptPath: path.join(webRoot, 'receipt.json'), sourceSha: 'source-a', env: { VITE_MOCK: '1' }, args: [] }
  return { dist, options }
}

function changeReceipt(options, changes) {
  const receipt = JSON.parse(readFileSync(options.receiptPath, 'utf8'))
  writeFileSync(options.receiptPath, JSON.stringify({ ...receipt, ...changes }))
}

function rejected(options, reason) {
  assert.deepEqual(reusablePrebuilt(options), { ok: false, reason })
}

test('record then reuse the same source, environment, arguments and bundle', t => {
  const { dist, options } = fixture(t)
  recordPrebuilt(options)
  const receipt = JSON.parse(readFileSync(options.receiptPath, 'utf8'))
  assert.equal(receipt.schema, RECEIPT_SCHEMA)
  assert.equal(receipt.source_sha, options.sourceSha)
  assert.equal(receipt.fingerprint, buildFingerprint(options.env, options.args))
  assert.equal(receipt.dist_digest, distDigest(dist))
  assert.ok(Number.isFinite(Date.parse(receipt.written_at)))
  assert.deepEqual(reusablePrebuilt(options), { ok: true, reason: 'reused' })
})

test('no-receipt-path for absent, empty or relative receipt paths', t => {
  const { options } = fixture(t)
  for (const receiptPath of [undefined, '', 'receipt.json']) {
    rejected({ ...options, receiptPath }, 'no-receipt-path')
  }
})

test('missing receipt file', t => {
  rejected(fixture(t).options, 'missing')
})

test('unreadable receipt JSON', t => {
  const { options } = fixture(t)
  writeFileSync(options.receiptPath, '{invalid')
  rejected(options, 'unreadable')
})

test('unreadable receipt at or above 64 KiB', t => {
  const { options } = fixture(t)
  writeFileSync(options.receiptPath, ' '.repeat(64 * 1024))
  rejected(options, 'unreadable')
})

test('schema mismatch', t => {
  const { options } = fixture(t)
  recordPrebuilt(options)
  changeReceipt(options, { schema: 'other' })
  rejected(options, 'schema')
})

test('source mismatch', t => {
  const { options } = fixture(t)
  recordPrebuilt(options)
  rejected({ ...options, sourceSha: 'source-b' }, 'source')
})

test('unknown source cannot reuse even a matching receipt', t => {
  const { options } = fixture(t)
  options.sourceSha = 'unknown'
  recordPrebuilt(options)
  rejected(options, 'source')
})

test('environment fingerprint mismatch', t => {
  const { options } = fixture(t)
  recordPrebuilt(options)
  rejected({ ...options, env: { VITE_MOCK: '0' } }, 'fingerprint')
})

test('argument fingerprint mismatch', t => {
  const { options } = fixture(t)
  recordPrebuilt(options)
  rejected({ ...options, args: ['--debug'] }, 'fingerprint')
})

test('dist-changed after editing a bundle file', t => {
  const { dist, options } = fixture(t)
  recordPrebuilt(options)
  writeFileSync(path.join(dist, '_next/static/a.js'), 'changed')
  rejected(options, 'dist-changed')
})

test('manifest source mismatch even when the dist digest matches', t => {
  const { dist, options } = fixture(t)
  writeFileSync(path.join(dist, 'web-build-manifest.json'), JSON.stringify({ source_sha: 'source-b' }))
  recordPrebuilt(options)
  rejected(options, 'manifest')
})

test('manifest must parse even when the dist digest matches', t => {
  const { dist, options } = fixture(t)
  writeFileSync(path.join(dist, 'web-build-manifest.json'), '{invalid')
  recordPrebuilt(options)
  rejected(options, 'manifest')
})

test('digest hashes sorted POSIX paths, lengths and raw bytes', t => {
  const { dist } = fixture(t)
  const hash = createHash('sha256')
  for (const relative of ['_next/static/a.js', 'index.html', 'web-build-manifest.json']) {
    const bytes = readFileSync(path.join(dist, relative))
    hash.update(relative + '\0' + bytes.length + '\0').update(bytes)
  }
  assert.equal(distDigest(dist), hash.digest('hex'))
})

test('missing dist throws for digest and fails closed for reuse', t => {
  const { dist, options } = fixture(t)
  recordPrebuilt(options)
  rmSync(dist, { recursive: true })
  assert.throws(() => distDigest(dist))
  rejected(options, 'dist-changed')
})

test('symlink entries throw for digest and fail closed for reuse', t => {
  const { dist, options } = fixture(t)
  recordPrebuilt(options)
  symlinkSync(path.join(dist, 'index.html'), path.join(dist, 'linked.html'))
  assert.throws(() => distDigest(dist), /non-regular/)
  rejected(options, 'dist-changed')
})

test('fingerprint is canonical and includes build settings but ignores unrelated env', () => {
  const env = { VITE_MOCK: '1', NODE_ENV: 'production', LEAF_ELEMENT_REF_STAMP: '0' }
  const fingerprint = buildFingerprint(env)
  assert.match(fingerprint, /^[a-f0-9]{64}$/)
  assert.equal(fingerprint, buildFingerprint({ LEAF_ELEMENT_REF_STAMP: '0', NODE_ENV: 'production', VITE_MOCK: '1', UNRELATED: 'ignored' }))
  for (const changes of [{ NODE_ENV: 'development' }, { LEAF_ELEMENT_REF_STAMP: '1' }, { VITE_CAD_EDIT: '1' }]) {
    assert.notEqual(fingerprint, buildFingerprint({ ...env, ...changes }))
  }
  assert.notEqual(buildFingerprint(env, ['a', 'b']), buildFingerprint(env, ['b', 'a']))
})
