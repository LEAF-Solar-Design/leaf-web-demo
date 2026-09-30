// Build the real app with the CAD surface present on both sides of the fence.
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const WEB_ROOT = process.cwd()
const MARKERS = ['solar-settings-form', 'Choose the drawing units before starting a Solar design.']

function emittedJavaScript(directory) {
  const chunks = []
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) chunks.push(...emittedJavaScript(path))
    else if (entry.name.endsWith('.js')) chunks.push(readFileSync(path, 'utf8'))
  }
  return chunks
}

function buildWithFlag(root, flag) {
  const outDir = join(root, flag === '1' ? 'on' : 'off')
  execFileSync(process.execPath,
    [join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', outDir, '--emptyOutDir'],
    {
      cwd: WEB_ROOT,
      env: { ...process.env, NODE_ENV: 'production', VITE_CAD_EDIT: '1', VITE_SOLAR_SETTINGS_FORM: flag, VITE_SOLAR_FLOW_RAIL: flag },
      stdio: 'pipe',
      timeout: 240_000,
    })
  const chunks = emittedJavaScript(outDir)
  expect(chunks.length).toBeGreaterThan(0)
  return chunks.join('\n')
}

describe('Solar settings build fence', () => {
  let root
  let onText
  let offText

  beforeAll(() => {
    root = mkdtempSync(join(tmpdir(), 'solar-settings-fence-'))
    onText = buildWithFlag(root, '1')
    offText = buildWithFlag(root, '0')
  }, 600_000)

  afterAll(() => {
    if (root) rmSync(root, { recursive: true, force: true })
  })

  it('SF2 fence the flag-on build ships the settings form', () => {
    for (const marker of MARKERS) expect(onText).toContain(marker)
  })

  it('SF2 fence the flag-off build ships no settings form', () => {
    for (const marker of MARKERS) expect(offText).not.toContain(marker)
  })

  it('FR fence the flag-off build ships no Solar step rail', () => {
    expect(onText).toContain('solar-flow-rail')
    expect(offText).not.toContain('solar-flow-rail')
  })

  it('FL16 fence the flag-off build ships no Solar flow selector', () => {
    expect(onText).toContain('solar-flow-select')
    expect(offText).not.toContain('solar-flow-select')
  })

  it('SF2 fence the positive control ships in both builds', () => {
    expect(onText).toContain('solar-tool-form')
    expect(offText).toContain('solar-tool-form')
    for (const text of [onText, offText]) {
      expect(text).not.toContain('jsxDEV(')
      expect(text).not.toContain(WEB_ROOT.replace(/\\/g, '/'))
    }
  })
})
