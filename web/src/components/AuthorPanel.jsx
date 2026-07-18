import { useEffect, useRef, useState } from 'react'

// "Author a tool" — a text description -> POST /api/author -> a generated
// tool card + code preview, added to the tool list so it's immediately
// runnable (CONTRACT §4 /api/author, §6 item 4).
//
// `seed` (+ `seedSignal`) is the build-lane hook: when the nl-prompt router
// classifies a prompt as lane:"build", App prefills this description here so the
// author flow opens ready to generate. seedSignal bumps on each route so the
// same text can be re-seeded.
const EXAMPLES = [
  'count panels within 18in of the roof edge',
  'measure the total area of all panels',
  'select everything on the Panel Groups layer',
]

export default function AuthorPanel({ onAuthor, onRunAuthored, seed, seedSignal }) {
  const [desc, setDesc] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [authored, setAuthored] = useState(null)
  const lastSignal = useRef(null)

  // Prefill from a build-lane route (only when the signal changes, so manual
  // edits are never clobbered by a re-render).
  useEffect(() => {
    if (seedSignal != null && seedSignal !== lastSignal.current) {
      lastSignal.current = seedSignal
      if (seed) setDesc(seed)
    }
  }, [seed, seedSignal])

  async function submit() {
    if (!desc.trim()) return
    setBusy(true); setErr(null); setAuthored(null)
    try {
      const res = await onAuthor(desc.trim())
      setAuthored(res)
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="author-panel author-inner">
      <p className="panel-sub">Describe a CAD tool in plain English. Leaf generates it and adds it to the catalog.</p>
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        placeholder="e.g. count panels within 18in of the roof edge"
        rows={3}
      />
      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="chip" onClick={() => setDesc(ex)}>{ex}</button>
        ))}
      </div>
      <button className="btn primary" disabled={busy || !desc.trim()} onClick={submit}>
        {busy ? 'Generating…' : 'Generate tool'}
      </button>
      {err && <div className="inline-error">{err}</div>}

      {authored && (
        <div className="authored">
          <div className="authored-head">
            <span className="tool-name">{authored.tool.name}</span>
            <span className="badge agent">agent</span>
          </div>
          <p className="authored-desc">{authored.preview || authored.tool.description}</p>
          <pre className="code"><code>{authored.code}</code></pre>
          <button className="btn" onClick={() => onRunAuthored(authored.tool, {})}>
            Run this tool
          </button>
        </div>
      )}
    </div>
  )
}
