import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(join(ROOT, 'src', 'App.jsx'), 'utf8')
const registry = readFileSync(join(ROOT, 'src', 'lib', 'actionRegistry.js'), 'utf8')
const jobs = readFileSync(join(ROOT, 'src', 'controllers', 'useJobController.js'), 'utf8')
const toolCast = readFileSync(join(ROOT, 'src', 'site', 'ToolCast.jsx'), 'utf8')
// Slice 6a: the version rows and the preview strip both shells render moved
// into ONE primitive. The pins below follow the behaviour to where it lives
// rather than being deleted with the file that used to hold it.
const versionList = readFileSync(join(ROOT, 'src', 'components', 'VersionList.jsx'), 'utf8')
const versionHistory = readFileSync(join(ROOT, 'src', 'components', 'VersionHistory.jsx'), 'utf8')
const styles = readFileSync(join(ROOT, 'src', 'styles.css'), 'utf8')
const landing = readFileSync(join(ROOT, 'src', 'site', 'landing.css'), 'utf8')
const demo = readFileSync(join(ROOT, 'src', 'demo', 'demo.css'), 'utf8')

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const detachStart = jobs.indexOf('const detachJob = useCallback(() => {')
const detachEnd = jobs.indexOf('const resumeJobPolling', detachStart)
const detachSource = jobs.slice(detachStart, detachEnd)
assert(
  detachStart >= 0 && detachEnd > detachStart &&
    detachSource.includes('sequenceRef.current += 1') &&
    !detachSource.includes('closeJobBeacon') &&
    toolCast.includes("if (!jobRunning) return undefined") &&
    toolCast.includes('detachJob()'),
  'Escape must detach the current run without sending the page-close beacon',
)
assert(
  jobs.includes("window.addEventListener('pagehide', closeInflight)") &&
    jobs.includes('servicesRef.current.closeJobBeacon(pointer.job_id)') &&
    jobs.includes('pointer.job_id === closedJobId'),
  'page hide must retain one deduplicated in-flight job close beacon',
)
// The R ladder moved into the action registry with standardization slice 10a
// (one record behind the ribbon, the engine ops, the slash picker and the key
// ladder). The CLAIM is unchanged and re-pointed at the file that now owns it:
// one keypress fires at most one rung, 'result' is never one of them (it is
// ResultPanel's own listener, and duplicating it sent two POST /api/run from a
// single keypress), and App still supplies every handler the rungs name.
// The RETRY_RUNGS table itself, sliced from its opening to its closing `})`,
// so the 'result' check reads the table and only the table, and tolerates
// whitespace: `result:`, `result :` and `result:(ctx)=>` all fail it.
const retryStart = registry.indexOf('export const RETRY_RUNGS = Object.freeze({')
const retryEnd = retryStart >= 0 ? registry.indexOf('})', retryStart) : -1
const retryTable = retryStart >= 0 && retryEnd > retryStart ? registry.slice(retryStart, retryEnd) : ''
assert(
  retryTable.length > 0 &&
    retryTable.includes("route: (ctx) => ctx.onRetryRoute?.()") &&
    retryTable.includes("history: (ctx) => ctx.onRetryHistory?.()") &&
    retryTable.includes("tools: (ctx) => ctx.onRetryTools?.()") &&
    retryTable.includes("catalog: (ctx) => ctx.onRetryCatalog?.()") &&
    retryTable.includes("refresh: (ctx) => ctx.onRetryRefresh?.()") &&
    // single-fire: the decision names ONE rung and the run dispatches ONE
    // handler out of the frozen table.
    registry.includes("if (rung) return { id: 'bar:retry'") &&
    registry.includes("RETRY_RUNGS[id](ctx)") &&
    // 'result' is absent from the table, so retryRung() cannot return it.
    !/\bresult\s*:/.test(retryTable) &&
    registry.includes("if (ctx.rTarget === 'result') return LADDER_REASONS.retryOwnedByResult") &&
    // App mounts the registry's listener over plain shell state and a handler
    // builder the listener calls only once a decision came back, and still
    // supplies every handler the rungs name.
    registry.includes('const decision = ladderDecision(event, shell)') &&
    app.includes('ladderListener(shell, ladderHandlers, markInstant)') &&
    app.includes('onRetryRoute: () => onDispatch()') &&
    app.includes('onRetryRefresh: () => onRetryViewerRefresh()'),
  'the R ladder must remain single-fire and cover its priority targets',
)
assert(
  app.includes('quotaAt > 0 && usageAt > quotaAt ? usage : null'),
  'quota notices must clear only after a newer successful usage read',
)
assert(
  jobs.includes('if (alive && isUnauthorized(cause))') &&
    jobs.includes('if (timer) clearInterval(timer)') && jobs.includes('timer = null'),
  'job polling must stop after an unauthenticated response',
)
assert(
  app.includes('const seq = (cannedSeq.current += 1)') &&
    app.includes('if (cannedSeq.current !== seq) return') &&
    app.includes('cannedSeq.current += 1   // kills any in-flight typing / dispatch'),
  'guided-tour cancellation must retain its sequence token',
)
assert(
  jobs.includes("export const INFLIGHT_JOB_KEY = 'leaf.inflightJob'") &&
    jobs.includes('storage?.setItem(INFLIGHT_JOB_KEY') &&
    jobs.includes("storage?.getItem(INFLIGHT_JOB_KEY) || 'null'"),
  'inflight jobs must retain the durable browser pointer used for reattach',
)
assert(
  toolCast.includes('const previewLocked = drawing.previewing != null') &&
    toolCast.includes('checkout.writeLocked || drawing.mutationsBlocked || previewLocked'),
  'previewing an older version must lock drawing writes on /try',
)
assert(
  versionList.includes('data-testid="try-preview-write-lock"') &&
    versionList.includes('Back to head') &&
    toolCast.includes('<VersionPreviewStrip') &&
    toolCast.includes('variant="tab"'),
  'the preview write lock must say it is locked and name the way back to head',
)
assert(
  versionList.includes('data-testid={`try-version-v${v}`}') &&
    versionList.includes('data-testid={`vh-row-v${v}`}') &&
    toolCast.includes('<VersionList') &&
    // /app reaches the primitive through the drawer, which is now chrome only.
    versionHistory.includes('<VersionList') &&
    app.includes('<VersionHistory'),
  'both shells must render the ONE version-list primitive, keeping their testids',
)
assert(
  !styles.includes('animation: none !important') && !landing.includes('animation: none !important'),
  'reduced motion must complete filled animations instead of canceling them',
)
// The scale tier may be the literal or the motion token (P0c): a tokenized
// sheet must DEFINE the token as .985 AND apply it, and (panel W0 finding)
// no rule may locally override the token to another value - substring
// co-occurrence alone would pass a sheet whose applying rule redefines it.
const scaleTier = (sheet) =>
  sheet.includes('transform: scale(.985)') ||
  (sheet.includes('--mo-scale-in: .985') &&
    sheet.includes('transform: scale(var(--mo-scale-in') &&
    !/--mo-scale-in:\s*(?!\.985)[\d.]/.test(sheet))
assert(
  !styles.includes('translateY(12px)') && !demo.includes('translateY(12px)') &&
    scaleTier(styles) && scaleTier(demo),
  'micro entrances must use the .985 scale tier (literal or tokenized) and never translated motion',
)

console.log('unified behavior pins passed')
