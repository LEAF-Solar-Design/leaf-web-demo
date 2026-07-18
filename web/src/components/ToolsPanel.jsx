import { useState } from 'react'

// Renders a JSON-Schema params object (CONTRACT §2 .params) as a small form.
function ParamForm({ schema, values, onChange }) {
  const props = schema?.properties || {}
  const keys = Object.keys(props)
  if (keys.length === 0) return <p className="params-none">No parameters.</p>
  return (
    <div className="params">
      {keys.map((k) => {
        const p = props[k]
        const label = p.title || k
        const val = values[k] ?? p.default ?? (p.type === 'number' ? 0 : '')
        return (
          <label key={k} className="param">
            <span>{label}</span>
            <input
              type={p.type === 'number' ? 'number' : 'text'}
              value={val}
              onChange={(e) =>
                onChange({ ...values, [k]: p.type === 'number' ? Number(e.target.value) : e.target.value })
              }
            />
          </label>
        )
      })}
    </div>
  )
}

function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined) out[k] = p.default
  }
  return out
}

export default function ToolsPanel({ tools, error, running, selectedTool, onRun, onOpenTool }) {
  const [openName, setOpenName] = useState(null)
  const [paramsByTool, setParamsByTool] = useState({})

  return (
    <div className="tools-inner">
      <p className="panel-sub">The classic catalog — click one, set params, run on Leaf. The prompt box above is the primary path.</p>
      {error && <div className="inline-error">Couldn’t load tools: {error}</div>}
      <div className="tool-list">
        {tools.map((t) => {
          const open = openName === t.name
          const params = paramsByTool[t.name] ?? defaultsOf(t.params)
          const isRunningThis = running && selectedTool?.name === t.name
          return (
            <div key={t.name} className={`tool-card ${open ? 'open' : ''}`}>
              <button
                className="tool-head"
                onClick={() => {
                  const next = open ? null : t.name
                  setOpenName(next)
                  onOpenTool?.(next ? t : null)
                }}
              >
                <div className="tool-head-main">
                  <span className="tool-name">{t.name}</span>
                  <span className={`badge ${t.provenance?.author === 'agent' ? 'agent' : 'user'}`}>
                    {t.provenance?.author || 'agent'}
                  </span>
                </div>
                <span className="tool-desc">{t.description}</span>
                <div className="tool-tags">
                  {(t.capabilities || []).map((c) => (
                    <span key={c} className={`cap ${c.includes('write') ? 'write' : 'read'}`}>{c}</span>
                  ))}
                  <span className="cap kind">{t.kind}</span>
                </div>
              </button>
              {open && (
                <div className="tool-body">
                  <ParamForm
                    schema={t.params}
                    values={params}
                    onChange={(v) => setParamsByTool((s) => ({ ...s, [t.name]: v }))}
                  />
                  <button
                    className="btn primary"
                    disabled={running}
                    onClick={() => onRun(t, params)}
                  >
                    {isRunningThis ? 'Running on Leaf…' : 'Run'}
                  </button>
                </div>
              )}
            </div>
          )
        })}
        {tools.length === 0 && !error && <div className="dim">Loading tools…</div>}
      </div>
    </div>
  )
}
