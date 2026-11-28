import React, { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { authorityKey } from '../lib/serviceAuthSession.js'
import { rememberOpenProject } from '../controllers/workspace/createWorkspaceController.js'
import { fileStem, fingerprintLine, runSendCopy, sendCopyMessage } from './sendCopy.js'

const PROGRESS = {
  workspace: 'Checking your workspace.',
  consent: 'Confirm in AutoCAD: a window asks before anything is sent.',
  uploading: 'Sending the copy to Studio.',
  reading: 'Reading your drawing in Studio.',
  project: 'Creating the project.',
  importing: 'Adding the drawing to the project.',
}
// Stops a person can do nothing about from here get no retry button.
const FINAL = new Set(['plugin_outdated', 'uploads_off', 'no_workspace', 'plan_lacks_upload', 'quota', 'too_large', 'no_model_objects'])
const OPEN_DELAY_MS = 2500

function stamp(date) {
  const pad = (value) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * M3 S4: one button that sends a consented copy of the open, unconnected DWG to the signed-in
 * account's own workspace and opens Studio on it. The DWG is not changed or connected.
 */
export default function OpenInStudioPanel({ bridge, disabled = false, onBusyChange = () => {},
  services = api, authority = authorityKey, remember = rememberOpenProject,
  navigate = (href) => globalThis.window?.location.assign(href), openDelayMs = OPEN_DELAY_MS }) {
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const [stopped, setStopped] = useState(null)
  const [done, setDone] = useState(null)
  const live = useRef(true)
  const run = useRef(null)

  useEffect(() => () => { live.current = false; run.current = null }, [])
  useEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])
  useEffect(() => {
    if (!done) return undefined
    const timer = setTimeout(() => navigate(done.href), openDelayMs)
    return () => clearTimeout(timer)
  }, [done, navigate, openDelayMs])

  async function start(options = {}) {
    if (busy || disabled || run.current) return
    const token = {}
    run.current = token
    setBusy(true)
    setStopped(null)
    setDone(null)
    try {
      const result = await runSendCopy({ bridge, api: services, authority, rememberProject: remember,
        onProgress: (phase) => { if (live.current && run.current === token) setProgress(PROGRESS[phase] || '') },
        ...options })
      if (!live.current || run.current !== token) return
      setDone(result)
    } catch (error) {
      if (!live.current || run.current !== token) return
      setStopped({ code: error?.code || 'unknown', sent: error?.sent ?? null, upload: error?.upload ?? null,
        serverMessage: error?.serverMessage ?? null, autocad: error?.autocad ?? null, studio: error?.studio ?? null })
    } finally {
      if (live.current && run.current === token) {
        run.current = null
        setBusy(false)
        setProgress('')
      }
    }
  }

  const fileName = stopped?.sent?.fileName ?? done?.fileName ?? null
  const message = stopped ? sendCopyMessage(stopped.code, { fileName, serverMessage: stopped.serverMessage }) : ''
  const reuse = stopped?.sent && stopped?.upload?.studio ? { sent: stopped.sent, upload: stopped.upload } : null

  return <div className="leaf-platform-summary leaf-platform-send" aria-label="Open in Studio">
    <h2>Open this drawing in Studio</h2>
    <p>Sends a copy to your own workspace and opens it in Leaf Automation Studio. Your DWG is not changed. Sending again makes a new copy.</p>
    {!done && <div className="leaf-platform-actions">
      <button type="button" disabled={busy || disabled} onClick={() => start()}>{busy ? 'Sending...' : 'Open in Studio'}</button>
    </div>}
    <p aria-live="polite">{busy ? progress : ''}</p>
    {stopped && <div role="alert" className="leaf-platform-send-stop">
      <p>{message}</p>
      {stopped.code === 'import_failed' && reuse && <button type="button" disabled={busy} onClick={() => start(reuse)}>Try again</button>}
      {stopped.code === 'project_forbidden' && reuse && <button type="button" disabled={busy} onClick={() => start({ ...reuse,
        project: { name: `${fileStem(stopped.sent.fileName)} ${stamp(new Date())}`, requireCreated: true } })}>Create a separate project</button>}
      {['lost_reply', 'read_timeout'].includes(stopped.code) && <p><a href="/app">Open Studio to check</a></p>}
      {(stopped.code === 'fingerprint_differs' || stopped.code === 'fingerprint_unavailable') && stopped.autocad && <dl className="leaf-platform-fingerprint">
        <dt>AutoCAD</dt><dd>{fingerprintLine(stopped.autocad, fileName)}</dd>
        <dt>Studio</dt><dd>{stopped.studio && stopped.studio.status === 'ok' ? fingerprintLine(stopped.studio, fileName) : 'not counted'}</dd>
      </dl>}
      {!FINAL.has(stopped.code) && !(['import_failed', 'project_forbidden'].includes(stopped.code) && reuse) &&
        <button type="button" disabled={busy || disabled} onClick={() => start()}>{stopped.code === 'lost_reply' || stopped.code.startsWith('fingerprint_') ? 'Send again' : 'Try again'}</button>}
    </div>}
    {done && <div className="leaf-platform-send-done">
      <p>Sent and checked. Opening Studio{done.workspace ? ` in ${done.workspace}` : ''}.</p>
      <dl className="leaf-platform-fingerprint" aria-label="Drawing fingerprint">
        <dt>AutoCAD</dt><dd>{fingerprintLine(done.autocad, done.fileName)}</dd>
        <dt>Studio</dt><dd>{fingerprintLine(done.studio, done.fileName)}</dd>
      </dl>
      <div className="leaf-platform-actions"><button type="button" onClick={() => navigate(done.href)}>Open now</button></div>
    </div>}
  </div>
}
