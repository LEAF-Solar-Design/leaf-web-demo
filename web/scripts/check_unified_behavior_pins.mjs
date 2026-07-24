import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(join(ROOT, 'src', 'App.jsx'), 'utf8')

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

assert(
  app.includes('runSeqRef.current += 1') && app.includes('if (!mock && currentJobId) closeJobBeacon(currentJobId)'),
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
  app.includes("if (e?.status === 401)") && app.includes('clearInterval(id); id = null'),
  'job polling must stop after an unauthenticated response',
)
assert(
  app.includes('const seq = (cannedSeq.current += 1)') &&
    app.includes('if (cannedSeq.current !== seq) return') &&
    app.includes('cannedSeq.current += 1   // kills any in-flight typing / dispatch'),
  'guided-tour cancellation must retain its sequence token',
)
assert(
  app.includes("const INFLIGHT_KEY = 'leaf.inflightJob'") &&
    app.includes('localStorage.setItem(INFLIGHT_KEY') &&
    app.includes("localStorage.getItem(INFLIGHT_KEY) || 'null'"),
  'inflight jobs must retain the durable browser pointer used for reattach',
)

console.log('unified behavior pins passed')
