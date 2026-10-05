import { readFile } from 'node:fs/promises'

const root = new URL('../', import.meta.url)
const api = await readFile(new URL('src/api.js', root), 'utf8')
const panel = await readFile(new URL('src/components/AuthorPanel.jsx', root), 'utf8')
const app = await readFile(new URL('src/App.jsx', root), 'utf8')
const toolCast = await readFile(new URL('src/site/ToolCast.jsx', root), 'utf8')
const authorController = await readFile(new URL('src/controllers/useAuthorStageController.js', root), 'utf8')
const authorPointer = await readFile(new URL('src/authorStagePointer.js', root), 'utf8')

function assert(ok, message) {
  if (!ok) throw new Error(message)
}

assert(api.includes("'/api/author/stage'"), 'live authoring must use the canonical R5 stage route')
assert(api.includes("'/api/author/publication-requests'"), 'live authoring must use the publication continuation route')
assert(!api.includes("'/api/author/register'"), 'browser authoring must not call the internal register route')
assert(!api.includes("'/api/author/confirmations'"), 'browser authoring must not request confirmation material')
assert(!app.includes('authorTool('), 'the live build flow must not call legacy authorTool')
assert(panel.includes('Staged and ready to publish. It is not runnable until publication succeeds.'), 'initial staged state must not claim approval is required')
assert(panel.includes('Awaiting independent approval. It remains staged and is not runnable.'), 'awaiting state must be explicit')
assert(panel.includes('Publication was denied. The staged tool was not published.'), 'denied state must be calm and explicit')
assert(panel.includes('Request publication') && panel.includes('Check approval & resume') && panel.includes('Revise') && panel.includes('Discard draft'), 'publication lifecycle actions must be explicit')
assert(!panel.includes('Tool authored'), 'live staged panel must not claim legacy authoring success')
assert(app.includes('useAuthorStageController') && app.includes('publishStagedAuthor'), 'app must separate staging from publishing')
assert(toolCast.includes('useAuthorStageController') && toolCast.includes('publishStagedAuthor'), 'unified surface must separate staging from publishing')
assert(authorController.includes('authorPointerValid') && authorPointer.includes('leaf.inflightAuthor.v1'), 'author staging must use its own scoped durable pointer')
assert(authorController.includes('initial.prior_staged ? { forceFresh: true } : {}'), 'revised staged requests must ask for fresh turn authority')
assert(toolCast.includes('forceFresh = false') && toolCast.includes('!forceFresh && cached &&'), 'unified author authority must bypass its cache for revisions')
assert(panel.includes('seedAutoSubmit && !stageActivity?.pointer && !stageActivity?.draftOnly'), 'pending requests and prepared drafts must never auto-submit from a seed')
assert(panel.includes('disabled={authorLocked || !!stageActivity?.pointer}') && panel.includes('Finish or discard the current request before cancelling the revision.'), 'revision cancellation must explain why a pending pointer blocks it')

console.log('customization web checks passed')
