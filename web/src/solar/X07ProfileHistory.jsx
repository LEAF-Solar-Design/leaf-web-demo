import { useEffect, useRef, useState } from 'react'

const positiveVersion = value => Number.isSafeInteger(value) && value > 0
const validChange = change => typeof change?.role === 'string' &&
  (change.field === null || typeof change.field === 'string') &&
  ['added', 'changed', 'removed'].includes(change.operation) &&
  Object.hasOwn(change, 'old') && Object.hasOwn(change, 'new')

function readPage(data, prior) {
  if (!data || !Array.isArray(data.rows) || data.rows.length > 50) throw new Error('Invalid history page')
  const { profile_id: profileId, through_version: throughVersion, rows, next_cursor: cursor } = data
  if (profileId === null && throughVersion === null && rows.length === 0 && cursor === null && !prior) return data
  const first = prior ? prior.rows.at(-1).version - 1 : throughVersion
  if (typeof profileId !== 'string' || !positiveVersion(throughVersion) || !rows.length ||
      (prior && (prior.profile_id !== profileId || prior.through_version !== throughVersion)) ||
      !rows.every((row, index) => positiveVersion(row?.version) && row.version === first - index &&
        typeof row.created_at === 'string' && Array.isArray(row.changes) && row.changes.every(validChange)) ||
      (rows.at(-1).version === 1
        ? cursor !== null
        : typeof cursor !== 'string' || !cursor.length || cursor.length > 256 || cursor === prior?.next_cursor)) {
    throw new Error('Invalid history page')
  }
  return prior ? { ...data, rows: [...prior.rows, ...rows] } : data
}

function displayValue(value) {
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

export default function X07ProfileHistory({ client, isCurrent }) {
  const request = useRef(null)
  const [page, setPage] = useState(null)
  const [pending, setPending] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => () => {
    request.current?.abort()
    request.current = null
  }, [])

  async function load() {
    if (request.current || !isCurrent()) return
    const controller = new AbortController()
    request.current = controller
    setPending(true)
    setFailed(false)
    try {
      const cursor = page?.next_cursor ?? undefined
      const result = await client.readRoleProfileHistory({ cursor, signal: controller.signal })
      if (controller.signal.aborted || !isCurrent()) return
      if (result?.ok !== true) throw new Error('History unavailable')
      setPage(readPage(result.data, page))
    } catch {
      if (!controller.signal.aborted && isCurrent()) setFailed(true)
    } finally {
      if (request.current === controller) {
        request.current = null
        setPending(false)
      }
    }
  }

  return <section aria-label="Profile history">
    {page?.rows.length > 0 && <ol>
      {page.rows.map(row => <li key={row.version}>
        <strong>Version {row.version}</strong>{' '}
        <time dateTime={row.created_at}>{row.created_at}</time>
        {row.changes.length ? <ul>{row.changes.map(change => <li key={JSON.stringify([change.role, change.field])}>
          {change.role}{change.field === null ? '' : `.${change.field}`}
          {change.operation === 'added' && <> added {displayValue(change.new)}</>}
          {change.operation === 'removed' && <> removed {displayValue(change.old)}</>}
          {change.operation === 'changed' && <> changed from {displayValue(change.old)} to {displayValue(change.new)}</>}
        </li>)}</ul> : <p>No role changes.</p>}
      </li>)}
    </ol>}
    {pending && <p role="status">Reading profile history.</p>}
    {failed && <p role="alert">Profile history could not be read.</p>}
    {page && page.rows.length === 0 && <p role="status">No saved profile revisions.</p>}
    {(!page || page.next_cursor !== null) && <button type="button" disabled={pending} onClick={load}>
      {failed ? 'Retry profile history' : page ? 'Load older revisions' : 'View profile history'}
    </button>}
  </section>
}
