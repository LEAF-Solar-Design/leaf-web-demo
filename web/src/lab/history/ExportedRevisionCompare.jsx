import { useEffect, useRef, useState } from 'react'
import { subscribeBeforeLogout, subscribeTokenStored } from '../../auth.js'
import { canonicalJSON } from '../contract.js'

const parts = {
  identity: 'Revision identity',
  graph: 'Graph',
  layout: 'Layout',
  policy_reference: 'Policy reference',
  evaluations: 'Evaluations',
}
const sides = ['Before', 'After']

function readBytes(file) {
  if (typeof file.arrayBuffer === 'function') return file.arrayBuffer()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(file)
  })
}

function producers(file) {
  const authorized = new Map(file.authorizedEvaluations.map(({ run, complete_results }) => [run.run_id, complete_results]))
  return file.bundle.parts.evaluations.flatMap(({ run, complete_results: results }, index) =>
    Object.entries(results).filter(([, producer]) => producer && typeof producer === 'object').map(([job, producer]) => {
      const { corpus, suite, policy } = producer.context ?? {}
      const pins = [policy?.block_id, corpus?.material_id, suite?.material_id]
      const pinned = pins.every(value => typeof value === 'string' && value)
      const saved = authorized.get(run.run_id)
      const savedProducer = saved && Object.hasOwn(saved, job) ? saved[job] : null
      const digest = producer.result_sha256
      const matches = typeof digest === 'string' && savedProducer && canonicalJSON(savedProducer) === canonicalJSON(producer)
      return { job, run: run.run_id, digest, pins: pinned ? pins : null, matches,
        uploadedMatch: file.uploadedMatches[index].get(job) }
    }))
}

function groupProducers(before, after) {
  const groups = new Map()
  const files = [before, after]
  files.forEach((file, side) => {
    for (const row of producers(file)) {
      const key = row.pins ? JSON.stringify(row.pins) : Symbol()
      if (!groups.has(key)) groups.set(key, [[], []])
      groups.get(key)[side].push(row)
    }
  })
  return [...groups.values()].flatMap(([left, right]) => left.length === 1 && right.length === 1
    ? [[left[0], right[0]]]
    : [...left.map(row => [row, null]), ...right.map(row => [null, row])])
}

function Comparison({ before, after }) {
  const left = before.bundle.manifest.parts, right = after.bundle.manifest.parts
  const partRows = Object.entries(parts).map(([name, label]) => [label, left[name], right[name]])
  const rows = [['File bytes', before.fileDigest, after.fileDigest], ...partRows]
  const lineage = groupProducers(before, after)
  return <div style={{ overflowX: 'auto', overflowWrap: 'anywhere' }}>
    <p role="status">Both bundle manifests verified. {partRows.every(([, a, b]) => a === b) ? 'No canonical parts changed.' : 'Canonical parts changed.'}</p>
    <table>
      <caption>Exported revision digests</caption>
      <thead><tr><th scope="col">Part</th><th scope="col">Before SHA-256</th><th scope="col">After SHA-256</th><th scope="col">Comparison</th></tr></thead>
      <tbody>{rows.map(([label, a, b]) => <tr key={label}>
        <th scope="row">{label}</th><td><code>{a}</code></td><td><code>{b}</code></td><td>{a === b ? 'Same' : 'Changed'}</td>
      </tr>)}</tbody>
    </table>
    {lineage.length ? <>
      <p>The byte check hashes the exact report JSON plus a newline. Reformatting can change that check even when the report still matches the saved result.</p>
      <table>
        <caption>Producer lineage</caption>
        <thead><tr><th scope="col">Block / Corpus / Suite</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
        <tbody>{lineage.map(([a, b], index) => <tr key={index}>
          <th scope="row">{(a?.pins ?? b?.pins)?.join(' / ') ?? 'Unpinned'}</th>
          {[a, b].map((row, side) => <td key={side}>{row ? <>
            <div>Run {typeof row.run === 'string' ? row.run : 'ID unavailable'}</div>
            <div>Job {row.job}</div>
            <div><code>{typeof row.digest === 'string' ? row.digest : 'Digest unavailable'}</code></div>
            <div>{row.uploadedMatch === undefined ? 'Uploaded result byte check unavailable'
              : row.uploadedMatch ? 'Uploaded result bytes match declared digest' : 'Uploaded result bytes differ from declared digest'}</div>
            <div>{row.matches ? 'Matches server-verified result' : 'Saved result match unavailable'}</div>
          </> : 'No paired producer'}</td>)}
        </tr>)}</tbody>
      </table>
    </> : <p>These exports contain no complete producer reports.</p>}
  </div>
}

export default function ExportedRevisionCompare({ client }) {
  const [files, setFiles] = useState({})
  const requests = useRef({})
  useEffect(() => {
    const reset = () => { requests.current = {}; setFiles({}) }
    reset()
    const offLogout = subscribeBeforeLogout(reset)
    const offToken = subscribeTokenStored(reset)
    return () => { requests.current = {}; offLogout(); offToken() }
  }, [client])

  async function choose(side, file) {
    const scopeCurrent = client.captureBundleScope()
    const current = () => requests.current[side] === current && scopeCurrent()
    requests.current[side] = current
    const show = value => setFiles(previous => ({ ...previous, [side]: value }))
    if (file.size > 64 * 1024 * 1024) {
      show({ error: 'Bundle is too large.' })
      return
    }
    show({ pending: true })
    try {
      const bytes = await readBytes(file)
      if (!current()) return
      const result = await client.inspectBundle(bytes, current)
      if (current()) show(result.ok ? { ...result, scopeCurrent } : { error: result.detail })
    } catch {
      if (current()) show({ error: 'Could not read or verify the bundle.' })
    }
  }

  const before = files.Before?.scopeCurrent?.() ? files.Before : null
  const after = files.After?.scopeCurrent?.() ? files.After : null
  return <details>
    <summary>Compare exported revisions</summary>
    {sides.map(side => {
      const state = files[side]
      const identity = state?.scopeCurrent?.() ? state.bundle.parts.identity : null
      return <div key={side}>
        <label>{side} export <input type="file" accept="application/json,.json" onChange={event => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void choose(side, file)
        }} /></label>
        {state?.pending && <p role="status">Verifying {side.toLowerCase()} export.</p>}
        {state?.error && <p role="alert">{side} export: {state.error}</p>}
        {identity && <p>{side} export: {identity.doc_id} r{identity.revision}</p>}
      </div>
    })}
    {before && after && <Comparison before={before} after={after} />}
  </details>
}
