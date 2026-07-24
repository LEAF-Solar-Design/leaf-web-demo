import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(join(ROOT, 'src', 'App.jsx'), 'utf8')
const jobs = readFileSync(join(ROOT, 'src', 'controllers', 'useJobController.js'), 'utf8')
const styles = readFileSync(join(ROOT, 'src', 'styles.css'), 'utf8')
const landing = readFileSync(join(ROOT, 'src', 'site', 'landing.css'), 'utf8')
const demo = readFileSync(join(ROOT, 'src', 'demo', 'demo.css'), 'utf8')

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

assert(
  jobs.includes('sequenceRef.current += 1') &&
    jobs.includes('if (!mock && jobId) servicesRef.current.closeJobBeacon(jobId)'),
  'Escape must detach the current run and send the close beacon',
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
  !styles.includes('animation: none !important') && !landing.includes('animation: none !important'),
  'reduced motion must complete filled animations instead of canceling them',
)
assert(
  !styles.includes('translateY(12px)') && !demo.includes('translateY(12px)') &&
    styles.includes('transform: scale(.985)') && demo.includes('transform: scale(.985)'),
  'micro entrances must use the scale tier and never translated motion',
)

console.log('unified behavior pins passed')
