// Keep the native layer in the payload: Next's import parser treats layer() as media.
import { readFileSync, writeFileSync } from 'node:fs'

const source = new URL('../src/lab/contract/sources/leaf-tokens.css', import.meta.url)
const target = new URL('../src/lab/labTokensLayer.css', import.meta.url)
const expected = Buffer.concat([
  Buffer.from('@layer leaf-lab-tokens {\n', 'utf8'),
  readFileSync(source),
  Buffer.from('\n}\n', 'utf8'),
])

if (process.argv.slice(2).some(argument => argument !== '--check')) {
  throw new Error('Usage: node scripts/stage_lab_token_layer.mjs [--check]')
}

if (process.argv.includes('--check')) {
  let actual
  try {
    actual = readFileSync(target)
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  if (!actual?.equals(expected)) {
    console.error('Lab token layer is stale. Run node scripts/stage_lab_token_layer.mjs')
    process.exitCode = 1
  }
} else {
  writeFileSync(target, expected)
}
