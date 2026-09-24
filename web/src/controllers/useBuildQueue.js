// The builds poll (standardization slice 11a): GET /api/builds on a bounded
// cadence in live mode, every record validated through lib/buildQueue.js
// before it reaches a host, malformed ones DROPPED and counted, never rendered.
// Mock mode makes no request at all (the rail shows the in-session run only).
//
// Failure posture: a failed poll keeps the last good list and records one
// warning; a 401 stops the poll (the jobs poll already owns the auth gate, so
// this hook never flips it) until `resume()` is called.
//
// `status` (W6-E01) tells the rail how far to trust the list: 'idle' in mock
// mode; 'live' while polling is enabled and no refresh has failed since the
// last good one or since polling started (so it reads live from mount: an
// empty list with no failure is not stale); 'stale' after any non-401
// failure; 'auth' after a 401 stopped the poll. The last good list survives
// both degraded states; `resume()` recovers either one by polling again at once.
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
  const [status, setStatus] = useState(mock ? 'idle' : 'live')
  const listRef = useRef((services && services.listBuilds) || defaultListBuilds)
  listRef.current = (services && services.listBuilds) || defaultListBuilds

  useEffect(() => {
    if (mock) {
      setBuilds([])
      setWarnings([])
      setDropped(0)
      setStatus('idle')
      return undefined
    }
    // Leaving mock mode: an empty list with no failure yet is live, not idle.
    setStatus((s) => (s === 'idle' ? 'live' : s))
    let alive = true
    let timer = null
    // Out-of-order guard: each request is numbered, and a result applies only
    // when it is newer than the last applied one. After a 401 applies, nothing
    // else from this generation applies; `resume()` starts a fresh generation.
    let seq = 0
    let applied = 0
    let paused = false
    const tick = async () => {
      const mine = ++seq
      const mayApply = () => alive && !paused && mine > applied
      let body
      try {
        body = await listRef.current(undefined, limit)
      } catch (cause) {
        if (!mayApply()) return
        applied = mine
        if (isUnauthorized(cause)) {
          paused = true
          if (timer) {
            clearInterval(timer)
            timer = null
          }
          setStatus('auth')
          setWarnings(['builds: sign-in required'])
          return
        }
        setStatus('stale')
        setWarnings(['builds: poll failed'])
        return
      }
      if (!mayApply()) return
      applied = mine
      const { records, dropped: bad } = parseBuildRecords(body && body.builds)
      const serverWarnings = Array.isArray(body && body.warnings)
        ? body.warnings.filter((w) => typeof w === 'string' && w.length <= 200).slice(0, 20)
        : []
      setBuilds(records)
      setDropped(bad.length)
      setStatus('live')
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

  return { builds, warnings, dropped, status, runningCount: runningBuildCount(builds), resume }
}
