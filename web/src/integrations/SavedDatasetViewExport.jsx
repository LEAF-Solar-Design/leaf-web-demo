import { useLayoutEffect, useRef, useState } from 'react'
import { isImportedMaterialDataset } from './DatasetDerivationPanel.jsx'

function sameReference(left, right) {
  return left && right && left.id === right.id && left.version === right.version && left.digest === right.digest
}

function completeRead(read, reference) {
  return sameReference(read?.dataset_ref, reference) && ['ready', 'empty'].includes(read.status) &&
    read.next_cursor === null && read.query_coverage?.kind === 'complete_materialization' &&
    read.query_coverage.offset === 0 && read.query_coverage.matching_rows === read.rows.length
}

function download(snapshot) {
  const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  try {
    link.href = url
    link.download = snapshot.view.element.id + '-r' + snapshot.view.revision + '.json'
    document.body.appendChild(link)
    link.click()
  } finally {
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}

export function SavedDatasetViewExport({ projectId, record, dataset, read, preferences, unavailable, guard, scopeKey }) {
  const operation = useRef(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useLayoutEffect(() => {
    setBusy(false)
    setError('')
    return () => {
      operation.current?.abort()
      operation.current = null
    }
  }, [projectId, scopeKey, record, dataset, read, preferences, unavailable])

  if (record?.element.kind !== 'dataset.table' || !isImportedMaterialDataset(dataset) ||
      !sameReference(dataset.reference, record.element.dataset_ref)) return null
  const ready = !unavailable && completeRead(read, dataset.reference)
  async function save() {
    if (!ready || operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true)
    setError('')
    try {
      await guard(controller.signal)
      if (controller.signal.aborted) return
      download({
        schema: 'leaf.saved-dataset-view.v1',
        project_id: projectId,
        view: { revision: record.revision, element: record.element },
        dataset: { reference: dataset.reference, revision: dataset.revision, material_lineage: dataset.material_lineage },
        query_result: read,
        display_preferences: preferences,
      })
    } catch {
      if (!controller.signal.aborted) setError('Could not download this view. Check your access and try again.')
    } finally {
      if (operation.current === controller) {
        operation.current = null
        setBusy(false)
      }
    }
  }
  return <div>
    <button type="button" disabled={!ready || busy} onClick={save}>Download view snapshot (JSON)</button>
    {!ready && <p>A complete current query result is required to download this view.</p>}
    {error && <p role="alert">{error}</p>}
  </div>
}
