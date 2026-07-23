import { readFile } from 'node:fs/promises'

const root = new URL('../', import.meta.url)
const api = await readFile(new URL('src/api.js', root), 'utf8')
const panel = await readFile(new URL('src/components/AuthorPanel.jsx', root), 'utf8')
const app = await readFile(new URL('src/App.jsx', root), 'utf8')

function assert(ok, message) {
  if (!ok) throw new Error(message)
}

assert(api.includes("'/api/author/stage'"), 'live authoring must use the canonical R5 stage route')
assert(api.includes("'/api/author/register'"), 'live authoring must use the canonical R6 register route')
assert(api.includes("'/api/author/confirmations'"), 'publish must obtain a server confirmation')
assert(!app.includes('authorTool('), 'the live build flow must not call legacy authorTool')
assert(panel.includes('Staged and awaiting approval. It is not runnable until publication succeeds.'), 'staged state must say it is not runnable')
assert(panel.includes('Publish tool'), 'staged state must require an explicit Publish action')
assert(!panel.includes('Tool authored'), 'live staged panel must not claim legacy authoring success')
assert(app.includes('stageAuthorTool') && app.includes('publishStagedAuthor'), 'app must separate staging from publishing')

console.log('customization web checks passed')
