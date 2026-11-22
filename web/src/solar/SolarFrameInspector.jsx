import React, { useEffect, useRef, useState } from 'react'

const unavailable = 'Saved frame attributes could not be read. Refresh the saved layout and try again.'
const number = value => Number(value.toFixed(6)).toString()

export default function SolarFrameInspector(props) {
  const scope = props.scope
  const key = JSON.stringify([scope?.organization_id, scope?.project_id, scope?.drawing_id, scope?.input_version_id])
  return <FrameScope key={key} {...props} />
}

function FrameScope({ scope, client, disabled = false }) {
  const [page, setPage] = useState(null)
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(false)
  const pending = useRef(null)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; pending.current?.abort() }
  }, [])

  async function read(offset = 0) {
    if (pending.current || disabled) return
    const controller = new AbortController()
    pending.current = controller
    setBusy(true); setPage(null); setSelected(''); setError('')
    try {
      let answer
      try { answer = await client.inspectFrames({ ...scope }, { offset, signal: controller.signal }) } catch { answer = null }
      if (!alive.current || pending.current !== controller) return
      if (!answer?.ok) { setError(unavailable); return }
      setPage(answer.value); setSelected(answer.value.frames[0]?.handle ?? '')
    } finally {
      if (alive.current && pending.current === controller) {
        pending.current = null; setBusy(false)
      }
    }
  }

  const frame = page?.frames.find(item => item.handle === selected)
  const units = page?.drawing_units === 'm' ? 'metres' : page?.drawing_units === 'ft' ? 'feet' : page?.drawing_units
  return <section aria-label="Saved Solar frame attributes">
    <h3>Generated frame attributes</h3>
    <p>Saved layout snapshot. Unsaved CAD edits are not included.</p>
    <button type="button" disabled={disabled || busy} onClick={() => read(page?.offset ?? 0)}>Read saved frame attributes</button>
    {busy && <p role="status">Reading saved frame attributes.</p>}
    {error && <p role="alert">{error}</p>}
    {page && <>
      <p role="status">{page.total ? 'Frames ' + (page.offset + 1) + ' to ' + (page.offset + page.frames.length) + ' of ' + page.total : 'No generated frames are saved.'}</p>
      <button type="button" disabled={disabled || busy || page.offset === 0} onClick={() => read(Math.max(0, page.offset - page.page_size))}>Previous frames</button>
      <button type="button" disabled={disabled || busy || page.offset + page.frames.length >= page.total} onClick={() => read(page.offset + page.page_size)}>Next frames</button>
      {page.frames.length > 0 && <label>Generated frame<select value={selected} disabled={disabled || busy} onChange={event => setSelected(event.target.value)}>
        {page.frames.map(item => <option key={item.handle} value={item.handle}>Frame {item.handle}, row {item.row}, column {item.column}</option>)}
      </select></label>}
      {frame && <>
        <dl>
          <dt>CAD handle</dt><dd>{frame.handle}</dd>
          <dt>Row</dt><dd>{frame.row}</dd>
          <dt>Column</dt><dd>{frame.column}</dd>
          <dt>Drawing elevation ({units})</dt><dd>{number(frame.elevation)}</dd>
          <dt>Preset</dt><dd>{frame.preset ?? 'Unassigned'}</dd>
          <dt>Pile template</dt><dd>{frame.pile_template ?? 'Unassigned'}</dd>
          <dt>Preset tilt (degrees)</dt><dd>{number(frame.pose.tilt_degrees)}</dd>
          <dt>Axis azimuth (degrees)</dt><dd>{number(frame.pose.axis_azimuth_deg)}</dd>
          <dt>Low pose height (metres)</dt><dd>{number(frame.pose.height_at_low_pose_m)}</dd>
          <dt>High pose height (metres)</dt><dd>{number(frame.pose.height_at_high_pose_m)}</dd>
        </dl>
        <p>Frame corners ({units})</p>
        <ol>{frame.vertices.map((point, index) => <li key={index}>{number(point[0])}, {number(point[1])}</li>)}</ol>
      </>}
    </>}
  </section>
}
