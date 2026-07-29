// Guard against a declaration being swallowed by a comment.
//
// App.jsx carries a ~140-line commented-out legacy block
// ("Legacy inline catalog dispatch is disabled; useCatalogController owns
// it."). Inserting a hook just inside it is easy, silent, and fatal: the JSX
// still references the binding, so the component throws ReferenceError on its
// FIRST render — and `npm run build` passes, because commented-out code is
// still valid syntax. Unit tests over pure modules miss it too, since they
// never render App.
//
// esbuild strips comments, so "does this declaration survive the transform"
// is a direct, cheap answer. Verified to fail on the exact commit where the
// declaration sat inside that block.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import esbuild from 'esbuild'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code

// Bindings App declares AND passes into JSX. Add a row whenever a new one is
// introduced; the cost is one line and the failure it catches is a white screen.
const DECLARED_AND_USED = [
  { name: 'slashCommandActions', usedAs: 'commandActions' },
  { name: 'registryEntries', usedAs: 'registryEntries' },
  { name: 'catalogSkills', usedAs: 'skills: catalogSkills' },
  { name: 'agentSessionId', usedAs: 'sessionId: agentSessionId' },
]

describe('App.jsx wiring', () => {
  for (const { name, usedAs } of DECLARED_AND_USED) {
    it(`declares ${name} in executable code, not inside a comment`, () => {
      // Matches a plain binding (`const x =`) and an array destructure
      // (`const [x, setX] =`), which is how useState results are bound.
      const declared = new RegExp(
        `(const|let|var)\\s+(\\[\\s*)?${name}\\s*[,\\]=]`).test(stripped)
        || new RegExp(
          `(const|let|var)\\s+\\{[^}]*\\b(?:\\w+\\s*:\\s*)?${name}\\s*[,}]`).test(stripped)
      assert.ok(declared,
        `${name} does not survive comment-stripping — its declaration is inside a ` +
        'comment block, so any render that reads it throws ReferenceError')
    })

    it(`actually uses ${name} (${usedAs}) after declaring it`, () => {
      assert.ok(stripped.includes(usedAs),
        `${usedAs} is not referenced in the compiled output — the wiring is dead`)
    })
  }

  it('passes sessionId into the PromptBox element itself', () => {
    // esbuild emits this JSX element as React.createElement(PromptBox, {...}).
    // Limit the match to that first props object, not another component call.
    assert.match(stripped,
      /React\.createElement\(\s*PromptBox,\s*\{[^}]*\bsessionId\s*:/)
  })
})
