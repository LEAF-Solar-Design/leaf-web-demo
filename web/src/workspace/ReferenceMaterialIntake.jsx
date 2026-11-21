import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeBeforeLogout } from '../auth.js'
import { listProjectReferences, readProjectReferenceOriginal, registerProjectArloInput, readProjectArloInput, activateProjectArloInput, reviseProjectArloInput, restoreProjectArloInput, uploadProjectReference } from '../projects/api.js'
import { humanizeError } from '../errorHumanize.js'

const message = error => humanizeError(error?.message || 'Reference files could not be loaded. Try again.')
const merge = (rows, added) => [...new Map([...rows, ...added].map(row => [row.material_id, row])).values()]

export default function ReferenceMaterialIntake({ projectId, orgId, onRunArlo }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  if (!projectId || !scope) return <section aria-label="Reference files">
    <h3>Reference files</h3><p>Sign in and open a project to add original reference files.</p>
  </section>
  return <ReferenceSession key={`${scope}:${orgId}:${projectId}`} projectId={projectId}
    orgId={orgId} scope={scope} onRunArlo={onRunArlo} />
}

function ReferenceSession({ projectId, orgId, scope, onRunArlo }) {
  const [rows, setRows] = useState([])
  const [queue, setQueue] = useState([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [cursor, setCursor] = useState(null)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState(null)
  const [arlo, setArlo] = useState(null)
  const [activeInputs, setActiveInputs] = useState({})
  const [editor, setEditor] = useState(null)
  const [constraintError, setConstraintError] = useState('')
  const constraintErrorId = useId()
  const constraintForm = useRef(null)
  useEffect(() => {
    const form = constraintForm.current
    if (!form) return
    // Native beforeinput runs before type=number can silently discard a comma.
    const rejectComma = event => {
      const path = event.target.dataset?.constraintPath
      const text = event.type === 'paste' ? event.clipboardData?.getData('text') : event.data
      if (path && typeof text === 'string' && text.includes(',')) {
        event.preventDefault()
        setConstraintError(`${path} uses a period for decimals. Commas are not accepted; enter the value again.`)
      }
    }
    form.addEventListener('beforeinput', rejectComma)
    form.addEventListener('paste', rejectComma)
    return () => {
      form.removeEventListener('beforeinput', rejectComma)
      form.removeEventListener('paste', rejectComma)
    }
  }, [editor?.materialId, editor?.inputId])
  const rejectedSaveFocus = useRef(null)
  useLayoutEffect(() => {
    if (arlo?.running) return
    const pending = rejectedSaveFocus.current
    rejectedSaveFocus.current = null
    if (constraintError && pending && !pending.moved && pending.button.isConnected
      && !pending.button.disabled && document.activeElement === document.body) {
      pending.button.focus()
    }
  }, [constraintError, arlo?.running])
  const input = useRef(null)
  const live = useRef(false)
  const running = useRef(false)
  const downloadRunning = useRef(false)
  const arloRunning = useRef(false)
  const arloKeys = useRef(new Map())
  const selections = useRef(new Map())
  const abort = useRef(null)
  const urls = useRef(new Map())
  const current = () => live.current && readActiveScope() === scope
  const options = () => ({ signal: abort.current.signal, expectedScope: scope })
  const load = async (next = null) => {
    if (!current()) return
    setLoading(true)
    setError('')
    try {
      const result = await listProjectReferences(projectId, { ...options(), cursor: next })
      if (!current()) return
      setRows(previous => next ? merge(previous, result.materials) : result.materials)
      setCursor(result.next_cursor || null)
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    } finally { if (current()) setLoading(false) }
  }
  useEffect(() => {
    live.current = true
    abort.current = new AbortController()
    void load()
    const clear = () => {
      live.current = false
      abort.current.abort()
      for (const [url, timer] of urls.current) { clearTimeout(timer); URL.revokeObjectURL(url) }
      urls.current.clear()
      arloKeys.current.clear()
      selections.current.clear()
      setArlo(null)
      setActiveInputs({}); setEditor(null); setConstraintError('')
      setRows([]); setQueue([]); setBusy(false); setLoading(false); setError(''); setDownloading(null)
    }
    const logout = subscribeBeforeLogout(clear)
    return () => { clear(); logout() }
  }, [projectId, scope])

  const run = async items => {
    if (running.current || !current()) return
    running.current = true
    setBusy(true)
    setQueue(items)
    setError('')
    const update = (index, state, detail = '') => setQueue(previous => previous.map((item, i) => i === index ? { ...item, state, detail } : item))
    try {
      for (let index = 0; index < items.length && current(); index++) {
        const item = items[index]
        if (item.state === 'uploaded') continue
        update(index, 'uploading')
        try {
          const result = await uploadProjectReference(projectId, item.file, options())
          if (!current()) break
          setRows(previous => merge(previous, [result.material]))
          update(index, 'uploaded')
        } catch (cause) {
          if (!current()) break
          update(index, 'failed', message(cause))
        }
      }
    } finally { running.current = false; if (current()) setBusy(false) }
  }
  const choose = event => {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (!current() || running.current || loading || !files.length) return
    if (files.length > 20) { setError('Choose up to 20 reference files at a time.'); return }
    void run(files.map(file => ({ file, state: 'waiting', detail: '' })))
  }
  const download = async row => {
    if (!current() || downloadRunning.current) return
    downloadRunning.current = true
    setDownloading(row.material_id)
    setError('')
    try {
      const blob = await readProjectReferenceOriginal(projectId, row.material_id, options())
      if (!current()) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = row.name
      document.body.appendChild(link)
      link.click()
      link.remove()
      // Keep the URL long enough for the browser to start saving; revoke on logout too.
      const timer = setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url) }, 1000)
      urls.current.set(url, timer)
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    } finally { downloadRunning.current = false; if (current()) setDownloading(null) }
  }
  const validateArlo = (input, row) => {
    if (input?.project_id !== projectId || input.organization_id !== orgId
        || input.material_id !== row.material_id || input.original_sha256 !== row.sha256
        || input.request?.contract !== 'arlo_design_request_v1') {
      throw new Error('The ARLO input did not match this project reference.')
    }
    return input
  }
  const prepareArlo = async row => {
    const selection = selections.current.get(row.material_id)
    if (selection) {
      if (!selection.confirmed) throw new Error('Select a retained ARLO revision again before editing or running.')
      try {
        const input = await readProjectArloInput(projectId, selection.originalId, { orgId, ...options() })
        if (!current()) return null
        validateSelection(input, row, selection, true)
        setEditor(previous => previous?.materialId === row.material_id
          && (previous.inputId !== input.input_version_id || previous.inputSha !== input.input_sha256) ? null : previous)
        setActiveInputs(previous => ({ ...previous, [row.material_id]: input }))
        return input
      } catch (cause) {
        selection.confirmed = false
        if (current()) setEditor(null)
        throw cause
      }
    }
    const registered = validateArlo(await registerProjectArloInput(projectId, row.material_id, {
      orgId, key: `arlo-reference-${row.material_id}`, ...options(),
    }), row)
    if (!current()) return null
    const input = validateArlo(await readProjectArloInput(projectId, registered.input_version_id, {
      orgId, ...options(),
    }), row)
    if (!current()) return null
    if ((input.original_id || input.input_version_id) !== (registered.original_id || registered.input_version_id)) {
      throw new Error('The ARLO input did not match this original family.')
    }
    setEditor(previous => previous?.materialId === row.material_id
      && (previous.inputId !== input.input_version_id || previous.inputSha !== input.input_sha256) ? null : previous)
    setActiveInputs(previous => ({ ...previous, [row.material_id]: input }))
    return input
  }
  const validateSelection = (input, row, selection, retained = false) => {
    validateArlo(input, row)
    if (input.input_version_id !== selection.input_version_id || input.input_sha256 !== selection.input_sha256
        || (input.original_id || input.input_version_id) !== selection.originalId
        || (input.parent_id || null) !== (selection.parent_id || null)
        || !/^[a-f0-9]{64}$/i.test(input.input_sha256 || '')) {
      throw new Error('The retained ARLO revision did not match the selected input.')
    }
    if (retained && !retainedCandidates(input, row).some(candidate => candidate.input_version_id === selection.input_version_id
        && candidate.input_sha256 === selection.input_sha256 && (candidate.parent_id || null) === (selection.parent_id || null))) {
      throw new Error('The selected ARLO revision is no longer retained. Select a revision again.')
    }
  }
  const retainedCandidates = (input, row) => {
    const originalId = input.original_id || input.input_version_id
    if (!Array.isArray(input.retained_inputs)) return []
    return input.retained_inputs.filter(candidate => candidate && typeof candidate.input_version_id === 'string'
      && /^[a-f0-9]{64}$/i.test(candidate.input_sha256 || '')
      && (candidate.input_version_id === originalId ? !candidate.parent_id : Boolean(candidate.parent_id))
      && (!candidate.original_id || candidate.original_id === originalId)
      && (candidate.organization_id === undefined || candidate.organization_id === orgId)
      && (candidate.project_id === undefined || candidate.project_id === projectId)
      && (candidate.material_id === undefined || candidate.material_id === row.material_id)
      && (candidate.original_sha256 === undefined || candidate.original_sha256 === row.sha256))
  }
  const selectRevision = async (row, inputId) => {
    if (!current() || arloRunning.current || !inputId) return
    const active = activeInputs[row.material_id]
    const candidate = retainedCandidates(active, row).find(item => item.input_version_id === inputId)
    const selection = { ...candidate, input_version_id: inputId,
      originalId: active.original_id || active.input_version_id, confirmed: false }
    selections.current.set(row.material_id, selection)
    setEditor(null)
    setConstraintError('')
    arloRunning.current = true
    setArlo({ materialId: row.material_id, detail: 'Selecting retained ARLO revision…', running: true })
    try {
      if (!candidate) throw new Error('The selected ARLO revision is no longer retained.')
      const activated = await activateProjectArloInput(projectId, selection.originalId, inputId, { orgId, ...options() })
      if (!current()) return
      validateSelection(activated, row, selection)
      const canonical = await readProjectArloInput(projectId, selection.originalId, { orgId, ...options() })
      if (!current()) return
      validateSelection(canonical, row, selection, true)
      selection.confirmed = true
      setActiveInputs(previous => ({ ...previous, [row.material_id]: canonical }))
      setArlo({ materialId: row.material_id, detail: 'Retained ARLO revision selected.', running: false })
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setArlo({ materialId: row.material_id, error: message(cause) })
    } finally { arloRunning.current = false }
  }
  const loadRevisions = async row => {
    if (!current() || arloRunning.current) return
    arloRunning.current = true
    setArlo({ materialId: row.material_id, detail: 'Loading retained ARLO revisions…', running: true })
    try {
      await prepareArlo(row)
      if (current()) setArlo({ materialId: row.material_id, detail: 'ARLO revisions loaded.', running: false })
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setArlo({ materialId: row.material_id, error: message(cause) })
    } finally { arloRunning.current = false }
  }
  const runArlo = async row => {
    if (!current() || arloRunning.current || !onRunArlo || !orgId) return
    arloRunning.current = true
    const materialId = row.material_id
    const signal = abort.current.signal
    setConstraintError('')
    const status = (detail, running = true) => setArlo({ materialId, detail, running })
    status('Preparing ARLO input…')
    try {
      const input = await prepareArlo(row)
      if (!input || !current() || signal.aborted) return
      let key = arloKeys.current.get(input.input_version_id)
      if (!key) {
        key = crypto.randomUUID()
        arloKeys.current.set(input.input_version_id, key)
      }
      status('Running ARLO…')
      const result = await onRunArlo(input, { scope, signal, idempotencyKey: key })
      if (!current() || signal.aborted) return
      if (!result?.ok) throw new Error(result?.error?.message || 'ARLO could not finish this run. Try again.')
      status('ARLO result saved to this project.', false)
    } catch (cause) {
      if (current() && !signal.aborted && cause?.name !== 'AbortError') setArlo({ materialId, error: message(cause) })
    } finally {
      arloRunning.current = false
    }
  }
  const editConstraints = async row => {
    if (!current() || arloRunning.current) return
    arloRunning.current = true
    setConstraintError('')
    setArlo({ materialId: row.material_id, detail: 'Opening constraints…', running: true })
    try {
      const input = await prepareArlo(row)
      if (!input || !current()) return
      const fields = {}
      for (const group of ['requirements', 'budget']) {
        for (const [name, value] of Object.entries(input.request[group] || {})) {
          if (typeof value === 'number') fields[`${group}.${name}`] = String(value)
        }
      }
      setEditor({ materialId: row.material_id, inputId: input.input_version_id, inputSha: input.input_sha256, fields })
      setArlo({ materialId: row.material_id, detail: 'Constraints ready to edit.', running: false })
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setArlo({ materialId: row.material_id, error: message(cause) })
    } finally { arloRunning.current = false }
  }
  const saveConstraints = async (row, restore = false) => {
    if (!current() || arloRunning.current) return
    const input = activeInputs[row.material_id]
    if (!input) return
    if (!restore && editor?.inputId !== input.input_version_id) {
      setConstraintError('The active ARLO input changed. Reopen constraints and try again.')
      setArlo({ materialId: row.material_id, running: false })
      return
    }
    const button = document.activeElement
    const focus = !restore && button?.matches('form[aria-label="ARLO constraints"] button[type="submit"]')
      ? { button, moved: false } : null
    rejectedSaveFocus.current = focus
    const moved = event => {
      if (focus && event.target !== button
        && (event.type === 'pointerdown' || event.target !== document.body)) focus.moved = true
    }
    document.addEventListener('focusin', moved, { signal: abort.current.signal })
    document.addEventListener('pointerdown', moved, { signal: abort.current.signal })
    arloRunning.current = true
    setConstraintError('')
    setArlo({ materialId: row.material_id, detail: restore ? 'Restoring original…' : 'Saving constraints…', running: true })
    try {
      if (selections.current.has(row.material_id)) {
        const canonical = await prepareArlo(row)
        if (!canonical || !current()) return
        if (canonical.input_version_id !== input.input_version_id || canonical.input_sha256 !== input.input_sha256) {
          setEditor(null)
          throw new Error('The active ARLO input changed. Reopen constraints and try again.')
        }
      }
      let revised
      if (restore) {
        revised = await restoreProjectArloInput(projectId, input.input_version_id, { orgId, ...options() })
      } else {
        const patch = {}
        for (const [path, text] of Object.entries(editor.fields)) {
          const [group, name] = path.split('.')
          const value = Number(text)
          if (!text.trim() || !Number.isFinite(value)) throw new Error('Enter a finite number for every constraint.')
          if (value !== input.request[group][name]) (patch[group] ||= {})[name] = value
        }
        if (!Object.keys(patch).length) { setEditor(null); return }
        revised = await reviseProjectArloInput(projectId, input.input_version_id, patch, { orgId, ...options() })
      }
      if (!current()) return
      validateArlo(revised, row)
      if (selections.current.has(row.material_id)) {
        const selection = selections.current.get(row.material_id)
        if ((revised.original_id || revised.input_version_id) !== selection.originalId
            || (restore ? revised.input_version_id !== selection.originalId : revised.parent_id !== input.input_version_id)) {
          selection.confirmed = false
          setEditor(null)
          throw new Error('The ARLO input did not match this original family.')
        }
        selections.current.set(row.material_id, { ...revised, originalId: selection.originalId, confirmed: true })
      }
      setActiveInputs(previous => ({ ...previous, [row.material_id]: revised }))
      setEditor(null)
      setArlo({ materialId: row.material_id, detail: restore ? 'Original ARLO input restored.' : 'ARLO revision saved. Run ARLO to use these constraints.', running: false })
    } catch (cause) {
      // The transport already filters server detail; preserve numeric validation
      // messages such as budget.timeout_seconds instead of treating them as network timeouts.
      if (current() && cause?.name !== 'AbortError') setConstraintError(cause?.status === 422 ? cause.message : message(cause))
    } finally {
      document.removeEventListener('focusin', moved)
      document.removeEventListener('pointerdown', moved)
      arloRunning.current = false
      if (current()) setArlo(previous => ({ ...previous, running: false,
        detail: previous?.running ? '' : previous?.detail }))
    }
  }
  const failed = queue.filter(item => item.state === 'failed').length
  const uploaded = queue.filter(item => item.state === 'uploaded').length
  return <section aria-label="Reference files">
    <h3>Reference files</h3>
    <p>Original MOV and gzip JSONL corpora up to 64 MiB; UTF-8 TXT and JSON up to 8 MiB each. Originals stay with this shared project.</p>
    <p>Preview unavailable. Uploading does not start an author turn or build.</p>
    <input ref={input} type="file" aria-label="Original reference files" accept=".mov,.txt,.json,.jsonl.gz" multiple hidden disabled={busy || loading} onChange={choose} />
    <button type="button" disabled={busy || loading} onClick={() => input.current?.click()}>Add reference files</button>
    <p role="status">{busy ? `Uploading reference files: ${uploaded} of ${queue.length} uploaded.` : loading ? 'Loading reference files…' : `${rows.length} reference files in this project.${queue.length ? ` ${uploaded} uploaded; ${failed} need a retry.` : ''}`}</p>
    {error && <p role="alert">{error} <button type="button" disabled={busy || loading} onClick={() => void load()}>Reload reference files</button></p>}
    {failed > 0 && !busy && <button type="button" onClick={() => void run(queue)}>Retry failed reference files</button>}
    {queue.length > 0 && <ul aria-label="Reference upload progress">{queue.map((item, index) => <li key={index}>
      {item.file.name}: {item.state === 'failed' ? item.detail : item.state === 'uploaded' ? 'Uploaded' : item.state === 'uploading' ? 'Uploading…' : 'Waiting'}
    </li>)}</ul>}
    {rows.length > 0 && <ul aria-label="Saved reference files">{rows.map(row => <li key={row.material_id}>
      <span>{row.name}</span>{' '}<span>{row.byte_size} bytes · {row.media_type}</span>{' '}
      <span>Uploaded · Preview unavailable · Model not consumed</span>{' '}
      <small>ID: {row.material_id} · SHA-256: {row.sha256}</small>{' '}
      <time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString()}</time>{' '}
      <button type="button" disabled={downloading !== null} onClick={() => void download(row)}>
        {downloading === row.material_id ? 'Downloading…' : `Download original ${row.name}`}
      </button>
      {onRunArlo && orgId && row.media_type === 'application/json' && <>
        <button type="button" disabled={Boolean(arlo?.running)} onClick={() => void runArlo(row)}>
          Run ARLO with {row.name}
        </button>
        <button type="button" disabled={Boolean(arlo?.running)} onClick={() => void editConstraints(row)}>Edit constraints</button>
        <button type="button" disabled={Boolean(arlo?.running)} onClick={() => void loadRevisions(row)}>Load retained revisions for {row.name}</button>
        {activeInputs[row.material_id] && <>
          <p>Active input: {activeInputs[row.material_id].input_version_id} · SHA-256: {activeInputs[row.material_id].input_sha256}
            {' '}· Parent: {activeInputs[row.material_id].parent_id || 'Original'} · Original: {activeInputs[row.material_id].original_id || activeInputs[row.material_id].input_version_id}</p>
          {Array.isArray(activeInputs[row.material_id].retained_inputs) && <label>Retained ARLO revision for {row.name}
            <select aria-label={`Retained ARLO revision for ${row.name}`} disabled={Boolean(arlo?.running)} value=""
              onChange={event => void selectRevision(row, event.target.value)}>
              <option value="">Select a retained revision</option>
              {retainedCandidates(activeInputs[row.material_id], row).map(candidate => <option key={candidate.input_version_id} value={candidate.input_version_id}>
                {candidate.input_version_id} · SHA-256: {candidate.input_sha256} · Parent: {candidate.parent_id || 'Original'}
              </option>)}
            </select>
          </label>}
          <button type="button" disabled={Boolean(arlo?.running) || !activeInputs[row.material_id].parent_id}
            onClick={() => void saveConstraints(row, true)}>Restore original</button>
        </>}
        {editor?.materialId === row.material_id && <form ref={constraintForm} aria-label="ARLO constraints" onSubmit={event => { event.preventDefault(); void saveConstraints(row) }}>
          {Object.entries(editor.fields).map(([path, value]) => <label key={path}>{path}
            <input type="number" step="any" data-constraint-path={path} value={value} disabled={Boolean(arlo?.running)}
              aria-describedby={constraintError ? constraintErrorId : undefined}
              aria-invalid={constraintError && (constraintError.startsWith(`${path} `)
                || !value.trim() || !Number.isFinite(Number(value))) ? true : undefined}
              onChange={event => { setConstraintError(''); setEditor(previous => ({ ...previous, fields: { ...previous.fields, [path]: event.target.value } })) }} />
          </label>)}
          <button type="submit" disabled={Boolean(arlo?.running)}>Save revision</button>
          <button type="button" disabled={Boolean(arlo?.running)} onClick={() => { setEditor(null); setConstraintError('') }}>Cancel editing</button>
        </form>}
        {arlo?.materialId === row.material_id && constraintError && <p id={constraintErrorId} role="alert">{constraintError}</p>}
        {arlo?.materialId === row.material_id && (arlo.error
          ? <p role="alert">{arlo.error}</p> : <p role="status">{arlo.detail}</p>)}
      </>}
    </li>)}</ul>}
    {cursor && <button type="button" disabled={loading || busy} onClick={() => void load(cursor)}>Load more reference files</button>}
  </section>
}
