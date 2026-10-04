import React, { useEffect, useRef, useState } from 'react'
import SolarReadResult from './SolarReadResult.jsx'
import { validateTerrainView } from './solarTerrainModel.js'
import { READ_RESULT_UNREADABLE, labelOf } from './solarReadResultModel.js'
import { PHYSICAL_READ_TOOLS, PHYSICAL_EXPORT_FORMATS, buildPhysicalRead, physicalReadData, physicalReadReason, sameHead } from './solarPhysicalReadModel.js'

const MOVED = 'The terrain changed, so review the refreshed preview before running again'
const NO_HEAD = 'No physical state has been published for this drawing.'
const UNAVAILABLE = 'The current physical state could not be read, so publishing stays off.'
const SHADE_DESCRIPTION = 'Report CPU terrain shade at native frame centres in the current physical head. Uses default clearance and an automatic sample profile. Reads only. Excludes weather weighting and individual-module shading.'
const EXPORT_DESCRIPTION = 'Download one terrain or CPU terrain shade CSV from the requested physical head. Shade samples native frame centres using default clearance and automatic profiles. Excludes weather weighting and individual-module shading.'

export default function SolarPhysicalReadPanel(props) {
  return <ReadScope key={JSON.stringify([props.drawingId ?? null, props.projectId ?? null, props.drawingVersion ?? null])} {...props} />
}

function ReadScope({ drawingId, projectId = null, drawingVersion, terrainClient, runRead, headSignal, disabled = false, download, save }) {
  const [phase, setPhase] = useState(drawingId ? 'loading' : 'no-drawing')
  const [view, setView] = useState(null)
  const [toolName, setTool] = useState(PHYSICAL_READ_TOOLS[0])
  const [format, setFormat] = useState(PHYSICAL_EXPORT_FORMATS[0])
  const [result, setResult] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const [announcement, setAnnouncement] = useState('')
  const [busy, setBusy] = useState(false)
  const alive = useRef(false)
  const epoch = useRef(0)
  const readGeneration = useRef(0)
  const readController = useRef(null)
  const lock = useRef(null)
  const attempts = useRef(0)
  const runButton = useRef(null)
  const focusAfter = useRef(null)

  useEffect(() => {
    const target = focusAfter.current
    if (!busy && target) {
      focusAfter.current = null
      if (document.activeElement === target || document.activeElement === document.body) target.focus()
    }
  }, [busy])

  async function read(refresh = false, preserve = false) {
    const generation = ++readGeneration.current
    const at = epoch.current
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    setPhase(refresh ? 'refreshing' : 'loading'); setView(null)
    if (!preserve) setRefusal(null)
    let answer
    try { answer = await terrainClient?.getTerrain({ drawingId, projectId, signal: controller.signal }) } catch { answer = null }
    if (!alive.current || at !== epoch.current || generation !== readGeneration.current) return null
    const value = answer?.ok ? validateTerrainView(answer.value, { drawingId, projectId }) : null
    if (value === null) { setPhase('refused'); if (!preserve) setRefusal(UNAVAILABLE); return null }
    setView(value); setPhase('ready')
    return value
  }

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; epoch.current++; readController.current?.abort() }
  }, [])
  useEffect(() => {
    epoch.current++; setResult(null); setAnnouncement('')
    if (drawingId) void read(false)
    else { setPhase('no-drawing'); setView(null) }
    return () => { readController.current?.abort() }
  }, [drawingId, projectId, drawingVersion, headSignal, terrainClient])

  async function refresh() {
    if (lock.current || !drawingId) return
    setResult(null)
    await read(true)
  }

  async function submit(event) {
    event.preventDefault()
    if (lock.current || disabled || !drawingId || view === null) return
    const token = { epoch: epoch.current, attempt: ++attempts.current }
    lock.current = token
    const target = runButton.current
    const restore = document.activeElement === target
    const current = () => alive.current && lock.current === token && token.epoch === epoch.current
    setBusy(true); setResult(null); setRefusal(null); setAnnouncement('A run is in progress, so wait for it to finish.')
    try {
      const captured = await read(true)
      if (!current()) return
      if (captured === null) return
      const built = buildPhysicalRead({ toolName, drawingId, head: captured.head, format })
      if (!built.ok) { setRefusal(built.reason === 'no_head' ? NO_HEAD : physicalReadReason()); setPhase('refused'); return }
      setPhase('pending')
      let answer
      try { answer = await runRead(toolName, built.params, drawingId, { projectId, dwgVersion: drawingVersion }) }
      catch { answer = null }
      if (!current()) return
      const accepted = physicalReadData(answer, { toolName, drawingId, projectId, drawingVersion, head: captured.head, format })
      // Every completed tool read is followed by one current-head read. There is no automatic tool retry.
      const latest = await read(true, true)
      if (!current()) return
      if (accepted.reason === 'head_moved' || (latest && !sameHead(latest.head, captured.head))) {
        setRefusal(MOVED); setAnnouncement(MOVED); setPhase('refused'); return
      }
      if (!latest) { setRefusal(UNAVAILABLE); setPhase('refused'); return }
      if (!accepted.ok) {
        const sentence = accepted.reason === 'unreadable' ? READ_RESULT_UNREADABLE : physicalReadReason()
        setRefusal(sentence); setAnnouncement(sentence); setPhase('refused'); return
      }
      setResult({ data: accepted.data, attempt: token.attempt }); setPhase('result'); setAnnouncement('result')
    } finally {
      if (alive.current && lock.current === token) {
        lock.current = null; setBusy(false)
        if (token.epoch === epoch.current && restore && (document.activeElement === target || document.activeElement === document.body)) focusAfter.current = target
      }
    }
  }

  if (!drawingId) return <section><p>Open a drawing to view terrain previews</p></section>
  return <section aria-label="Physical reads" data-state={phase}>
    <p>{SHADE_DESCRIPTION}</p><p>{EXPORT_DESCRIPTION}</p>
    {phase === 'loading' && <p>Reading the current physical state.</p>}
    {phase === 'refreshing' && <p>Refreshing the current physical state.</p>}
    {disabled && <p>A run is in progress, so wait for it to finish.</p>}
    {view && !view.head && <p>{NO_HEAD}</p>}
    <form onSubmit={submit}>
      <label htmlFor="physical-read-tool">{labelOf('tool')}</label>
      <select id="physical-read-tool" value={toolName} disabled={busy || disabled} onChange={(e) => { setTool(e.target.value); setResult(null) }}>
        {PHYSICAL_READ_TOOLS.map((tool) => <option key={tool} value={tool}>{labelOf(tool.replaceAll('-', ' '))}</option>)}
      </select>
      {toolName === 'solar-physical-export' && <>
        <label htmlFor="physical-read-format">{labelOf('format')}</label>
        <select id="physical-read-format" value={format} disabled={busy || disabled} onChange={(e) => { setFormat(e.target.value); setResult(null) }}>
          {PHYSICAL_EXPORT_FORMATS.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </>}
      <button ref={runButton} type="submit" disabled={busy || disabled || view === null || view.head === null}>Run</button>
      <button type="button" onClick={refresh} disabled={busy}>Refresh physical state</button>
    </form>
    {refusal && <p role="alert">{refusal}</p>}
    <p role="status" aria-live="polite">{announcement}</p>
    {result && <SolarReadResult key={JSON.stringify([drawingId, projectId, drawingVersion, result.attempt, result.data.job_id, result.data.output_sha256])}
      data={result.data} download={download} save={save} />}
  </section>
}
