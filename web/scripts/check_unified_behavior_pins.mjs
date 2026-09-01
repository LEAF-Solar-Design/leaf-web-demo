import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(join(ROOT, 'src', 'App.jsx'), 'utf8')
const jobs = readFileSync(join(ROOT, 'src', 'controllers', 'useJobController.js'), 'utf8')
const toolCast = readFileSync(join(ROOT, 'src', 'site', 'ToolCast.jsx'), 'utf8')
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
assert(
  app.includes("rTarget && rTarget !== 'result'") &&
    app.includes("if (rTarget === 'route') onDispatch()") &&
    app.includes("else if (rTarget === 'refresh') onRetryViewerRefresh()"),
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
  toolCast.includes('data-testid="try-preview-write-lock"') &&
    toolCast.includes('Back to head'),
  'the preview write lock must say it is locked and name the way back to head',
)
assert(
  !styles.includes('animation: none !important') && !landing.includes('animation: none !important'),
  'reduced motion must complete filled animations instead of canceling them',
)
// The scale tier may be the literal or the motion token (P0c): a tokenized
// sheet must still DEFINE the token as .985 for the pin to hold.
const scaleTier = (sheet) =>
  sheet.includes('transform: scale(.985)') ||
  (sheet.includes('--mo-scale-in: .985') && sheet.includes('transform: scale(var(--mo-scale-in'))
assert(
  !styles.includes('translateY(12px)') && !demo.includes('translateY(12px)') &&
    scaleTier(styles) && scaleTier(demo),
  'micro entrances must use the .985 scale tier (literal or tokenized) and never translated motion',
)

console.log('unified behavior pins passed')
