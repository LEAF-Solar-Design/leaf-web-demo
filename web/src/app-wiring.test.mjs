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
const promptBoxSessionBinding = /React\.createElement\(\s*PromptBox,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/
const conversePanelSessionBinding = /React\.createElement\(\s*ConversePanel,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/

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
    // Limit the match to the PromptBox props object, not another component
    // receiving the same session binding.
    assert.match(stripped, promptBoxSessionBinding)
  })

  it('rejects PromptBox sessionId={null} even while ConversePanel keeps its binding', () => {
    const mutated = appSource.replace(
      /(<PromptBox[\s\S]*?\bsessionId=\{)agentSessionId(\})/,
      '$1null$2',
    )
    assert.notEqual(mutated, appSource, 'the falsification mutation must target PromptBox')
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    assert.match(mutatedStripped, conversePanelSessionBinding)
    assert.doesNotMatch(mutatedStripped, promptBoxSessionBinding)
  })

  it('disables image paste in the catalog command bar instead of dropping an attachment', () => {
    assert.match(stripped, /imageAttachmentsEnabled:\s*false/)
    const promptBox = readFileSync(new URL('./components/PromptBox.jsx', import.meta.url), 'utf8')
    assert.match(promptBox, /if \(!imageAttachmentsEnabled\)/)
    assert.match(promptBox, /Image paste is available in the assistant reply box/)
  })

  it('waits for the authoritative published catalog tool before exposing it to Run it now', () => {
    assert.match(
      stripped,
      /refreshedTools\s*=\s*await getTools\(false\)[\s\S]*publishedTool\?\.catalog_digest[\s\S]*upsertTool\(tool\)[\s\S]*await loadCatalog\(\)/,
    )
  })

  it('seats live session intake with the durable drawing version summary', () => {
    assert.match(
      stripped,
      /drawingSummary\s*=\s*await getDrawingVersions\(false,\s*DRAWING_SOURCE\)[\s\S]*drawingId:\s*DRAWING_SOURCE,\s*drawingState:\s*drawingSummary/,
    )
  })
})
