import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, realpath } from 'node:fs/promises'
import { basename, isAbsolute, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = fileURLToPath(new URL('../../../', import.meta.url))

export const repoPath = (value) => typeof value === 'string' && value.trim().length > 0
  && !value.includes('\\') && !value.includes(':') && !value.includes('\0')
  && value.split('/').every((part) => part && part !== '.' && part !== '..')

export async function verifiedFixture(fixture) {
  assert.ok(repoPath(fixture.path), 'fixture.path: expected repo-relative POSIX path')
  const root = await realpath(repoRoot)
  const path = await realpath(resolve(root, fixture.path))
  const relativePath = relative(root, path)
  assert.ok(relativePath && relativePath !== '..' && !relativePath.startsWith('../')
    && !relativePath.startsWith('..\\') && !isAbsolute(relativePath), 'fixture must stay inside the repo')
  const buffer = await readFile(path)
  assert.equal(buffer.length, fixture.bytes, `${fixture.path}: byte count`)
  assert.equal(createHash('sha256').update(buffer).digest('hex'), fixture.sha256, `${fixture.path}: SHA-256`)
  // Upload the checked bytes rather than reopening a path after verification.
  return { name: basename(fixture.path), mimeType: 'application/octet-stream', buffer }
}
