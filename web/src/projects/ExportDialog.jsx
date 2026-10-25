/**
 * ExportDialog — streams a sanitized project artifact to the browser.
 *
 * Card B-U4 acceptance:
 *   Export streams/downloads the sanitized artifact; progress bounded with
 *   timeout surfaced as an unknown outcome; receipt id shown.
 *
 * Purely props-driven for its data contract (Membership.jsx pattern): the
 * caller supplies `onExport`, the only thing that talks to a server. This
 * component owns just the lifecycle around that one call — progress, the
 * timeout bound, and turning the resolved artifact into a real download.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import EscCap from '../components/EscCap.jsx'
import { humanizeError } from '../errorHumanize.js'
import { useLifecycleDialog } from './CloneDialog.jsx'

// Own timeout class (mirrors fetchBudget.js's FetchTimeoutError) so a caller
// can tell "the export hung past its bound" apart from any other rejection.
export class ExportTimeoutError extends Error {
  constructor(timeoutMs) {
    super('We could not confirm whether the export finished. Check the project history before exporting again.')
    this.name = 'ExportTimeoutError'
    this.timeoutMs = timeoutMs
    this.retryable = false
  }
}

function describeExportError(e) {
  if (e?.name === 'ExportTimeoutError') return { message: humanizeError(e), retryable: false }
  const retryable = e?.retryable != null ? !!e.retryable : true
  const message = humanizeError(e?.body?.error?.message || e)
  return { message, retryable }
}

export default function ExportDialog({
  onExport, // required: async ({ signal, onProgress }) => { receiptId, blob, filename }
  onDismiss,
  timeoutMs = 30000,
  title = 'Export project',
}) {
  const [status, setStatus] = useState('idle') // idle | exporting | complete | error
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState(null)
  const [receiptId, setReceiptId] = useState(null)
  const [downloadUrl, setDownloadUrl] = useState(null)
  const [downloadName, setDownloadName] = useState('')

  // Bumped on every new attempt (and on unmount): a stale attempt's async
  // completion must never overwrite a newer one's state.
  const generationRef = useRef(0)
  const urlRef = useRef(null)
  const dialogRef = useLifecycleDialog(true, () => onDismiss?.(), 'export-project')

  const revokeDownload = useCallback(() => {
    if (!urlRef.current) return
    try { URL.revokeObjectURL(urlRef.current) } catch { /* best effort cleanup */ }
    urlRef.current = null
  }, [])

  useEffect(() => {
    return () => {
      generationRef.current += 1
      revokeDownload()
    }
  }, [revokeDownload])

  const runExport = useCallback(async () => {
    if (!onExport || status === 'exporting') return
    const generation = ++generationRef.current
    revokeDownload()
    setStatus('exporting')
    setProgress(null)
    setError(null)
    setReceiptId(null)
    setDownloadUrl(null)
    dialogRef.current?.focus()

    const controller = new AbortController()
    let timer = null
    // The whole export is bounded by timeoutMs — never an indefinite spinner.
    // Firing it both aborts the in-flight call (cooperative cancellation) and
    // wins the race below with an unknown outcome, not permission to retry.
    const timeoutPromise = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort()
        reject(new ExportTimeoutError(timeoutMs))
      }, timeoutMs)
    })

    const onProgress = (fraction) => {
      if (generationRef.current !== generation) return
      const n = Number(fraction)
      setProgress(Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : null)
    }

    try {
      const exportPromise = Promise.resolve(onExport({ signal: controller.signal, onProgress }))
      // A late resolve/reject after the timeout already won the race must not
      // surface as an unhandled rejection — the race above owns the outcome.
      exportPromise.catch(() => {})
      const result = await Promise.race([exportPromise, timeoutPromise])
      if (generationRef.current !== generation) return
      if (!result?.receiptId) throw new Error('Export completed without a receipt id.')
      if (result.blob) {
        const url = URL.createObjectURL(result.blob)
        urlRef.current = url
        setDownloadUrl(url)
        setDownloadName(result.filename || 'export')
      }
      setReceiptId(result.receiptId)
      setProgress(1)
      setStatus('complete')
    } catch (cause) {
      if (generationRef.current !== generation) return
      setError(describeExportError(cause))
      setStatus('error')
    } finally {
      clearTimeout(timer)
    }
  }, [dialogRef, onExport, revokeDownload, status, timeoutMs])

  return (
    <div className="export-dialog-backdrop" role="presentation">
    <section ref={dialogRef} tabIndex={-1} className="export-dialog" role="dialog" aria-modal="true" aria-label={title}>
      <div className="export-dialog-head">
        <span className="export-dialog-title">{title}</span>
        {onDismiss && (
          <EscCap type="button" onClick={onDismiss} label="Close" />
        )}
      </div>

      <p className="export-dialog-copy">
        Exports a sanitized copy of this project&apos;s artifact, with no draft or working-file
        state included.
      </p>

      {status === 'idle' && (
        <button type="button" className="chip-act" onClick={runExport} disabled={!onExport}>
          Export
        </button>
      )}

      {status === 'exporting' && (
        <div className="export-dialog-progress">
          <progress
            role="progressbar"
            aria-label="Export progress"
            aria-valuenow={progress == null ? undefined : Math.round(progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
            value={progress == null ? undefined : progress}
            max={1}
          />
          <span className="export-dialog-progress-label">{progress == null ? 'Preparing download...' : `${Math.round(progress * 100)}%`}</span>
        </div>
      )}

      {status === 'error' && error && (
        <div className="export-dialog-error" role="alert">
          <span>{error.message}</span>
          {error.retryable && (
            <button type="button" className="chip-act" onClick={runExport}>Retry</button>
          )}
        </div>
      )}

      {status === 'complete' && (
        <div className="export-dialog-complete">
          <p className="export-dialog-receipt">
            Receipt: <span className="export-dialog-receipt-id">{receiptId}</span>
          </p>
          {downloadUrl && (
            <a className="chip-act" href={downloadUrl} download={downloadName}>Download</a>
          )}
          <button type="button" className="chip-neutral" onClick={runExport}>Export again</button>
        </div>
      )}
    </section>
    </div>
  )
}
