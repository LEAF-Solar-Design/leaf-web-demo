// The builds poll (standardization slice 11a): GET /api/builds on a bounded
// cadence in live mode, every record validated through lib/buildQueue.js
// before it reaches a host, malformed ones DROPPED and counted, never rendered.
// Mock mode makes no request at all (the rail shows the in-session run only).
//
// Failure posture: a failed poll keeps the last good list and records one
// warning; a 401 stops the poll (the jobs poll already owns the auth gate, so
// this hook never flips it) until `resume()` is called.
import { useCallback, useEffect, useRef, useState } from 'react'

import { listBuilds as defaultListBuilds } from '../api.js'
import { parseBuildRecords, runningBuildCount } from '../lib/buildQueue.js'

const isUnauthorized = (error) => error?.status === 401 || / -> 401$/.test(String(error?.message || ''))

export default function useBuildQueue({
  mock = false,
  pollIntervalMs = 5000,
  limit = 20,
  services = null,
} = {}) {
  const [builds, setBuilds] = useState([])
  const [warnings, setWarnings] = useState([])
  const [dropped, setDropped] = useState(0)
  const [generation, setGeneration] = useState(0)
  const listRef = useRef((services && services.listBuilds) || defaultListBuilds)
  listRef.current = (services && services.listBuilds) || defaultListBuilds

  useEffect(() => {
    if (mock) {
      setBuilds([])
      setWarnings([])
      setDropped(0)
      return undefined
    }
    let alive = true
    let timer = null
    const tick = async () => {
      let body
      try {
        body = await listRef.current(undefined, limit)
      } catch (cause) {
        if (!alive) return
        if (isUnauthorized(cause) && timer) {
          clearInterval(timer)
          timer = null
        }
        setWarnings(['builds: poll failed'])
        return
      }
      if (!alive) return
      const { records, dropped: bad } = parseBuildRecords(body && body.builds)
      const serverWarnings = Array.isArray(body && body.warnings)
        ? body.warnings.filter((w) => typeof w === 'string' && w.length <= 200).slice(0, 20)
        : []
      setBuilds(records)
      setDropped(bad.length)
      setWarnings(bad.length ? [...serverWarnings, `builds: ${bad.length} malformed record(s) dropped`] : serverWarnings)
    }
    tick()
    timer = setInterval(tick, pollIntervalMs)
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [generation, limit, mock, pollIntervalMs])

  const resume = useCallback(() => setGeneration((g) => g + 1), [])

  return { builds, warnings, dropped, runningCount: runningBuildCount(builds), resume }
}
