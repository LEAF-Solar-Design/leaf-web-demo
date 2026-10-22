#!/usr/bin/env node
// A read-only vendor gate. Updating pins requires an explicit overlay review.
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(path.join(root, p))
const pin = JSON.parse(read('web/src/lab/widgets/widget-renderer.version'))
const fail = (message) => { throw new Error(message) }
if (process.argv.length !== 3 || process.argv[2] !== '--check') fail('Usage: sync-widget-renderer.mjs --check')
if (!/^[a-f0-9]{40}$/.test(pin.upstream_sha)) fail('Missing upstream sha')
const seen = new Set()
function visit(file) {
  if (seen.has(file)) return
  seen.add(file)
  const bytes = read(file), source = bytes.toString('utf8')
  if (createHash('sha256').update(bytes).digest('hex') !== pin.files[file]) fail('Renderer drift: ' + file)
  if (/\b(?:use[A-Z]\w*|fetch|XMLHttpRequest|localStorage|sessionStorage)\b|\b(?:window|document|globalThis)\s*[.\[]|\bimport\s*\(|\brequire\s*\(/.test(source)) fail('Renderer is not hook-free: ' + file)
  for (const match of source.matchAll(/(?:import|export)\s+(?:[^'";]*?\s+from\s*)?['"]([^'"]+)['"]/g)) {
    const specifier = match[1]
    if (!specifier.startsWith('./')) fail('External renderer import: ' + specifier)
    const target = path.posix.normalize(path.posix.join(path.posix.dirname(file), specifier))
    if (!Object.hasOwn(pin.files, target)) fail('Unpinned renderer import: ' + target)
    visit(target)
  }
}
for (const entry of pin.entries) visit(entry)
if (seen.size !== Object.keys(pin.files).length) fail('Pin includes files outside the import closure')
const sources = JSON.parse(read('contract/fixtures/widget/sources/SOURCES.json'))
for (const source of sources.sources) {
  const bytes = read(source.frozen_path)
  if (bytes.length !== source.bytes || createHash('sha256').update(bytes).digest('hex') !== source.sha256) fail('Frozen source drift: ' + source.frozen_path)
}
console.log('Widget renderer pins match')
