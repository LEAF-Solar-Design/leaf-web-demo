import assert from 'node:assert/strict'
import { test } from 'node:test'
import { inheritedOsEnvironment } from './stack.mjs'

test('inherited OS environment keeps the loader path and excludes ambient authority', () => {
  assert.deepEqual(inheritedOsEnvironment({
    PATH: '/opt/marker/bin',
    LD_LIBRARY_PATH: '/opt/marker/lib',
    LD_PRELOAD: '/opt/marker/evil.so',
    DATABASE_URL: 'postgres://test.invalid/main',
    AWS_SECRET_ACCESS_KEY: 'must-not-leak',
    ARBITRARY_NAME: 'must-not-leak',
  }), {
    PATH: '/opt/marker/bin',
    LD_LIBRARY_PATH: '/opt/marker/lib',
  })
})

test('inherited OS environment matches names case-insensitively and preserves their spelling', () => {
  assert.deepEqual(inheritedOsEnvironment({
    Path: '/opt/marker/bin',
    ld_library_path: '/opt/marker/lib',
    ld_preload: '/opt/marker/evil.so',
    database_url: 'postgres://test.invalid/main',
    aws_secret_access_key: 'must-not-leak',
    arbitrary_name: 'must-not-leak',
  }), {
    Path: '/opt/marker/bin',
    ld_library_path: '/opt/marker/lib',
  })
})
