import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { subscribeBeforeLogout } from '../auth.js'
import { authorityKey } from '../lib/serviceAuthSession.js'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from '../site/groundWindow.js'
import { createEstimatorBridge } from './estimatorBridge.js'
import { estimatorOriginAllowed, canonicalProject } from './estimatorOrigins.js'
import { mintEstimatorHandoff } from './estimatorRoute.js'
import { useEstimatorSurfaceStatus } from './useEstimatorSurfaceStatus.js'
import './estimator.css'

const FAILURE = 'The estimator did not respond. Retry'

export default function EstimatorGround({ active, projectId, accountScope, signedIn = false,
  mock = false, studioShell = false, occluders = NO_OCCLUDERS }) {
  const status = useEstimatorSurfaceStatus(mock)
  const binding = status?.state === 'on' && status.bindings?.find(row => row.project_id === projectId)
  const origin = binding?.estimator_origin
  const allowed = Boolean(!mock && signedIn && canonicalProject(projectId) && estimatorOriginAllowed(origin))
  const authority = authorityKey()
  const latest = useRef(null)
  latest.current = { projectId, accountScope, authority, allowed, origin }
  const live = useRef(null)
  const mounted = useRef(true)
  const nextKey = useRef(0)
  const recreates = useRef([])
  const [frame, setFrame] = useState(null)
  const [view, setView] = useState('loading')
  const [reopen, setReopen] = useState(false)
  const [retryAt, setRetryAt] = useState(0)
  const board = useRef(null)
  const win = useGroundWindow(active, true, board, occluders)

  const current = record => {
    const next = latest.current
    return live.current === record && !record.retiring && next.allowed
      && record.projectId === next.projectId && record.accountScope === next.accountScope
      && record.authority === next.authority && record.authority === authorityKey() && record.origin === next.origin
  }

  const retire = useCallback(() => {
    const record = live.current
    if (!record) return Promise.resolve()
    if (record.retiring) return record.retiring
    // The bridge cancels any in-flight mint synchronously before awaiting the peer.
    record.retiring = (record.bridge?.end() || Promise.resolve()).then(() => {
      record.bridge?.dispose()
      if (live.current === record) {
        live.current = null
        if (mounted.current) setFrame(null)
      }
    })
    return record.retiring
  }, [])

  useLayoutEffect(() => {
    mounted.current = true
    // Development StrictMode replays effects without rerunning a retained ref.
    // A retired bridge must never strand that frame or mint twice on replay.
    if (!live.current) setFrame(null)
    const off = subscribeBeforeLogout(() => {
      setReopen(true)
      setView('loading')
      return live.current ? retire() : undefined
    })
    return () => {
      mounted.current = false
      off()
      live.current?.bridge?.end()
      live.current?.bridge?.dispose()
      live.current = null
    }
  }, [retire])

  useLayoutEffect(() => {
    if (live.current && !current(live.current)) {
      setReopen(true)
      setView('loading')
      retire()
    }
  }, [projectId, accountScope, authority, allowed, origin, retire])

  useEffect(() => { setReopen(false) }, [active])
  useEffect(() => {
    if (!retryAt) return
    const timer = setTimeout(() => setRetryAt(0), Math.max(0, retryAt - Date.now()))
    return () => clearTimeout(timer)
  }, [retryAt])

  useLayoutEffect(() => {
    if (live.current || frame || !active || !allowed || reopen || view !== 'loading') return
    const record = { key: ++nextKey.current, projectId, accountScope, authority, origin, bridge: null, retiring: null }
    live.current = record
    setFrame(record)
  }, [frame, active, allowed, reopen, view, projectId, accountScope, authority, origin])

  const attach = useCallback(node => {
    if (!node || !frame) return
    if (frame.bridge) return
    frame.bridge = createEstimatorBridge({
      frame: node, origin: frame.origin, isCurrent: () => current(frame),
      mint: (chan, signal) => mintEstimatorHandoff(frame.projectId, chan, { signal }),
      onState: next => {
        if (!current(frame)) return
        setView(next)
        if (next === 'throttled') setRetryAt(Date.now() + 60000)
        if (next !== 'ready') retire()
      },
      onRecreate: () => {
        if (!current(frame)) return
        frame.bridge?.dispose()
        live.current = null
        setFrame(null)
        const now = Date.now()
        recreates.current = recreates.current.filter(at => at > now - 300000)
        if (recreates.current.length >= 3) { setView('failed'); return }
        recreates.current.push(now)
        setView('loading')
      },
    })
  }, [frame, retire])

  const retry = async () => {
    if (retryAt) return
    await retire()
    if (!mounted.current) return
    setReopen(false)
    setView('loading')
  }
  const message = !signedIn ? 'Sign in to open the estimator.'
    : status?.state === 'on' && !binding ? 'No estimator is linked to this project'
      : view === 'denied' ? 'You do not have access to the estimator for this project'
        : view === 'throttled' ? 'Please wait 60 seconds before retrying the estimator.'
          : view === 'failed' || status?.state === 'off' || status?.state === 'unavailable' ? FAILURE
            : reopen ? 'Reopen the Estimator tab to continue.' : 'Loading estimator…'
  const frameVisible = frame && current(frame) && !['denied', 'failed', 'throttled'].includes(view)
  return <section ref={board} data-ground="estimator" className="estimator-ground" hidden={!active}
    aria-label="Estimator workspace" data-studio-shell={studioShell ? 'cockpit' : undefined}>
    <div className="estimator-window" data-measured={Boolean(win)} style={win ? windowStyle(win) : undefined}>
      {(!frameVisible || view !== 'ready') && <div className="estimator-status" role="status">
        <p>{message}</p>
        {signedIn && allowed && (reopen || ['denied', 'failed', 'throttled'].includes(view))
          && <button type="button" className="btn dp-focus" disabled={Boolean(retryAt)} onClick={retry}>Retry</button>}
      </div>}
      {frame && <iframe key={frame.key} ref={attach} src={`${frame.origin}/studio/enter`}
        hidden={!frameVisible} title="Project estimator" referrerPolicy="no-referrer" allow=""
        sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox" />}
    </div>
  </section>
}
