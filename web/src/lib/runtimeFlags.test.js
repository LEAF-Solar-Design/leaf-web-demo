// @vitest-environment node
//
// The one-shell runtime rail, end to end as source pins: the reader's
// fail-closed truth table, the default file, the load order, the entrypoint's
// fail-closed rewrite, the Dockerfile wiring and the no-store cache rule.
// Losing ANY link silently strands an environment on the wrong shell.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { readOneShellEnabled } from './runtimeFlags.js'

describe('readOneShellEnabled', () => {
  it("is true only for the literal string '1'", () => {
    expect(readOneShellEnabled({ __LEAF_FLAGS: { oneShell: '1' } })).toBe(true)
  })

  it('fails closed on every other shape', () => {
    expect(readOneShellEnabled({ __LEAF_FLAGS: { oneShell: '0' } })).toBe(false)
    expect(readOneShellEnabled({ __LEAF_FLAGS: { oneShell: 1 } })).toBe(false)
    expect(readOneShellEnabled({ __LEAF_FLAGS: { oneShell: 'true' } })).toBe(false)
    expect(readOneShellEnabled({ __LEAF_FLAGS: {} })).toBe(false)
    expect(readOneShellEnabled({})).toBe(false)
    expect(readOneShellEnabled(null)).toBe(false)
    expect(readOneShellEnabled(undefined)).toBe(false)
  })

  it('fails closed on a throwing accessor (storage-locked webviews)', () => {
    const hostile = {}
    Object.defineProperty(hostile, '__LEAF_FLAGS', { get() { throw new Error('locked') } })
    expect(readOneShellEnabled(hostile)).toBe(false)
  })
})

describe('rail wiring pins', () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8').replace(/\r\n/g, '\n')

  it('ships a default flags file with the shell OFF', () => {
    expect(read('../../public/runtime-flags.js')).toMatch(/window\.__LEAF_FLAGS = \{ oneShell: '0' \}/)
  })

  it('index.html loads the flags synchronously BEFORE the bundle', () => {
    const html = read('../../index.html')
    const flags = html.indexOf('<script src="/runtime-flags.js"></script>')
    const bundle = html.indexOf('<script type="module" src="/src/main.jsx">')
    expect(flags).toBeGreaterThan(-1)
    expect(bundle).toBeGreaterThan(-1)
    expect(flags).toBeLessThan(bundle)
  })

  it('the entrypoint rewrite fails closed to 0 on any non-1 value', () => {
    const sh = read('../../../deploy/write-runtime-flags.sh')
    expect(sh).toMatch(/one_shell="\$\{LEAF_ONE_SHELL_ENABLED:-0\}"/)
    expect(sh).toMatch(/case "\$one_shell" in\n {2}1\) ;;\n {2}\*\) one_shell=0 ;;/)
    expect(sh).toMatch(/> \/usr\/share\/nginx\/html\/runtime-flags\.js/)
  })

  it('the Dockerfile installs the entrypoint drop-in executably', () => {
    const dockerfile = read('../../../deploy/Dockerfile.web')
    expect(dockerfile).toMatch(/COPY deploy\/write-runtime-flags\.sh \/docker-entrypoint\.d\/40-runtime-flags\.sh/)
    expect(dockerfile).toMatch(/RUN chmod \+x \/docker-entrypoint\.d\/40-runtime-flags\.sh/)
  })

  it('nginx serves the flags file no-store, so a flag flip needs only a reload', () => {
    const nginx = read('../../../deploy/nginx.conf')
    expect(nginx).toMatch(/location = \/runtime-flags\.js \{\n {8}try_files \$uri =404;\n {8}add_header Cache-Control "no-store";/)
  })
})
