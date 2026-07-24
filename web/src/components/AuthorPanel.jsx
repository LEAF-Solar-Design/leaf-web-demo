import { useEffect, useRef, useState } from 'react'

// "Author a tool" — a text description -> POST /api/author -> a generated
// tool card + code preview, added to the tool list so it's immediately
// runnable (CONTRACT §4 /api/author, §6 item 4).
//
// Real authoring runs an agent and takes ~1–2 min, so Generate becomes a calm
// long-running state: plain ticking text ("Authoring with the agent · 34s"),
// NO spinner. On success we surface PROVENANCE — authored-by-agent vs templated,
// derived from the response `source` (sibling contract: top-level
// "harness"|"template"), with an honest "agent unavailable — templated fallback"
// when the harness was configured but unreachable — plus any advisory
// static-scan flags as calm amber square-dot states, and an NT2 toast so a
// completion after minutes of waiting is never invisible.
//
// `seed` (+ `seedSignal`) is the build-lane hook: when the nl-prompt router
// classifies a prompt as lane:"build", App prefills this description here so the
// author flow opens ready to generate. seedSignal bumps on each route so the
// same text can be re-seeded.
const EXAMPLES = [
  'count panels within 24in of the roof edge',
  'measure the total area of all panels',
  'select everything on the Panel Groups layer',
]

// Derive the honest provenance from the /api/author response. The sibling
// contract adds top-level `source` + `static_scan`; until it lands we fall back
// to the signals the current backend already emits (a preview fallback marker +
// tool.provenance.static_scan), so this reads correctly either way.
function readProvenance(res) {
  const preview = res?.preview || ''
  // The templated-fallback marker the backend prepends when the harness was
  // configured but unreachable — the honest "agent unavailable" signal.
  const fallback = /\[harness unreachable|templated fallback/i.test(preview) || res?.fallback === true
  const source = res?.source || null
  // Agent-authored ONLY when the server says source:"harness" and it wasn't a fallback.
  const agent = source === 'harness' && !fallback
  const scan = Array.isArray(res?.static_scan)
    ? res.static_scan
    : (res?.tool?.provenance?.static_scan || [])
  return { source, agent, fallback, scan }
}

// Widget-card spec: an authored (sandboxed) card carries a mono provenance line
// (run · sha · grants · reviewer) — built only from fields that actually exist
// on the response / tool.provenance, absent-safe.
function authoredProvLine(res) {
  const p = res?.tool?.provenance || {}
  const parts = []
  // Author + created lead the block — the two fields the runbook tells the
  // presenter to point at. Absent-safe, so a live response that omits them is
  // rendered exactly as before.
  if (p.author) parts.push(`author ${p.author}`)
  if (p.created) {
    const t = new Date(p.created)
    if (!Number.isNaN(t.getTime())) parts.push(`created ${t.toLocaleTimeString()}`)
  }
  const run = res?.run_id || p.run_id || p.session_id
  if (run) parts.push(`run ${String(run).slice(0, 12)}`)
  const sha = p.sha256 || p.sha || res?.sha256
  if (sha) parts.push(String(sha).slice(0, 12))
  if (p.grants !== undefined && p.grants !== null) {
    const grants = Array.isArray(p.grants) ? (p.grants.length ? p.grants.join(' ') : 'none') : String(p.grants)
    parts.push(`grants ${grants}`)
  }
  if (p.reviewer !== undefined) parts.push(`reviewer ${p.reviewer || '—'}`)
  // The params parsed out of the sentence (e.g. `distance_in: 18`) — the proof
  // the description was actually understood. Capped so a many-param live tool
  // can't run the line long; absent-safe for tools with no defaults.
  const props = res?.tool?.params?.properties || {}
  for (const [k, v] of Object.entries(props).slice(0, 3)) {
    if (v && v.default !== undefined && v.default !== '') parts.push(`${k}: ${v.default}`)
  }
  return parts.length ? parts.join(' · ') : null
}

// Calm authoring telemetry chip (3A). The /api/author response OPTIONALLY carries
// `telemetry` on a REAL agent build only — { turns, input_tokens, output_tokens,
// total_cost_usd, models[] }. It is ABSENT for template + fallback authoring, so
// this renders nothing unless genuine agent metrics are present (absent-safe). No
// alarm, no spinner — a quiet receipt of what the agent actually did.
function fmtAgentCost(usd) {
  const n = Number(usd)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `~$${n.toFixed(4)}` : `~$${n.toFixed(2)}`
}
function TelemetryChip({ telemetry }) {
  if (!telemetry || typeof telemetry !== 'object') return null
  const turns = Number(telemetry.turns)
  const cost = fmtAgentCost(telemetry.total_cost_usd)
  const model = Array.isArray(telemetry.models) && telemetry.models.length ? telemetry.models[0] : null
  const parts = ['agent']
  if (Number.isFinite(turns)) parts.push(`${turns} turn${turns === 1 ? '' : 's'}`)
  if (cost) parts.push(cost)
  if (model) parts.push(model)
  // Nothing beyond the bare "agent" tag -> stay silent (absent-safe).
  if (parts.length <= 1) return null
  const inT = Number(telemetry.input_tokens)
  const outT = Number(telemetry.output_tokens)
  const tokTitle = (Number.isFinite(inT) || Number.isFinite(outT))
    ? `tokens · in ${Number.isFinite(inT) ? inT.toLocaleString() : '—'} · out ${Number.isFinite(outT) ? outT.toLocaleString() : '—'}`
    : 'authoring-agent telemetry'
  return (
    <span className="telemetry-chip" role="status" title={tokTitle}>
      {parts.join(' · ')}
    </span>
  )
}

// Provenance status: dot + sentence-case word, never a pill. Hollow = authored
// but not in play as reviewed/trusted; amber square = the honest fallback
// advisory.
function ProvenanceStatus({ prov }) {
  if (prov.fallback) {
    return (
      <span className="tier-status advisory">
        <span className="dot square" aria-hidden="true" />
        Agent unavailable — templated fallback
      </span>
    )
  }
  if (prov.agent) {
    return (
      <span className="tier-status sandboxed">
        <span className="dot hollow" aria-hidden="true" />
        Authored by agent
      </span>
    )
  }
  return (
    <span className="tier-status sandboxed">
      <span className="dot hollow" aria-hidden="true" />
      Templated
    </span>
  )
}

// The calm authoring gate (CONCERN 2). Shown when the tenant has no linked Claude
// account — proactively (notLinked) or reactively (a grant-required rejection).
// It is a NUDGE, never a red failure: templated authoring stays fully usable, so
// the copy points at the Claude account panel without blocking the Generate flow.
// The gate's own ::before square dot carries the advisory — no label chip.
function AuthorGate({ onLinkClaude }) {
  return (
    <div className="author-gate" role="status">
      <div className="author-gate-body">
        <b>Link your Claude account to author with the agent.</b>{' '}
        Templated authoring still works below without it — linking your Claude subscription lets the
        real agent write richer tools.
        {onLinkClaude && (
          <div className="author-gate-act">
            <button className="chip-act" onClick={onLinkClaude}>Link Claude account</button>
          </div>
        )}
      </div>
    </div>
  )
}

// The REAL build-entitlement gate. Unlike the Claude-account nudge above, this is
// a plan boundary ENFORCED server-side (POST /api/author returns 403 when the
// tier lacks `build`) — so authoring is genuinely unavailable here (Generate is
// disabled), stated in plain words with no fake enforcement claim.
function BuildGate() {
  return (
    <div className="author-gate" role="status">
      <div className="author-gate-body">
        <b>Your plan doesn’t include tool authoring — upgrade to build.</b>{' '}
        Authoring new tools is a build-lane capability enforced on Leaf. Upgrade your plan to
        generate tools with the agent.
      </div>
    </div>
  )
}

// B2: the authoring agent's harness/broker was unreachable (a 502 / 503 / 504 or
// an explicit BROKER_UNREACHABLE code on POST /api/author). This is a transient
// infrastructure hiccup, not a user error and not a plan boundary — surface it as
// calm "service temporarily unavailable" copy, never a raw red failure.
function isServiceDown(e) {
  if (!e) return false
  if (e.status === 502 || e.status === 503 || e.status === 504) return true
  const code = String((e.body && e.body.error && e.body.error.error_code) || '').toUpperCase()
  if (code === 'BROKER_UNREACHABLE' || code === 'SERVICE_UNAVAILABLE' || code === 'HARNESS_UNREACHABLE') return true
  const msg = String((e.message || '')).toLowerCase()
  return /unreachable|service unavailable|bad gateway|gateway timeout|\b50[234]\b/.test(msg)
}

function ServiceGate() {
  return (
    <div className="author-gate" role="status">
      <div className="author-gate-body">
        <b>Authoring service is temporarily unavailable.</b>{' '}
        The tool-authoring agent couldn’t be reached just now. Nothing was charged —
        please try Generate again in a moment.
      </div>
    </div>
  )
}

function lifecycleMessage(error) {
  const code = String(error?.body?.reason_code || error?.body?.error?.code || error?.body?.error?.message || error?.message || '').toLowerCase()
  if (/independent_approval_pending|approval.pending/.test(code)) return 'An independent reviewer has not approved this exact staged change yet. It remains staged and is not runnable.'
  if (error?.status === 409 || /conflict|stale/.test(code)) return 'This staged change conflicts with a newer catalog. Stage it again to review the current base.'
  if (error?.status === 410 || /expired/.test(code)) return 'That publish approval expired. Stage the tool again to request a fresh approval.'
  if (error?.status === 503 || /not configured|unavailable|fail.closed/.test(code)) return 'Publishing is unavailable because the protected customization service is not ready. The staged tool was not published.'
  return 'Publish did not complete. The staged tool is not runnable.'
}

export default function AuthorPanel({ onAuthor, onPublish, onUseAuthored, seed, seedSignal, seedAutoSubmit = false, notLinked, onLinkClaude, buildEntitled = true }) {
  const [desc, setDesc] = useState('')
  const [busy, setBusy] = useState(false)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [err, setErr] = useState(null)
  const [grantGate, setGrantGate] = useState(false) // a grant-required rejection (calm gate, not red)
  const [buildGate, setBuildGate] = useState(false) // a build-entitlement rejection (calm plan gate, not red)
  const [svcGate, setSvcGate] = useState(false) // authoring service unreachable (B2; calm, not red)
  const [authored, setAuthored] = useState(null)
  const [publishing, setPublishing] = useState(false)
  const [publishErr, setPublishErr] = useState(null)
  const lastSignal = useRef(null)
  const startRef = useRef(null)
  const authoredRef = useRef(null)

  // Prefill from a build-lane route (only when the signal changes, so manual
  // edits are never clobbered by a re-render).
  useEffect(() => {
    if (seedSignal != null && seedSignal !== lastSignal.current) {
      lastSignal.current = seedSignal
      if (seed) {
        setDesc(seed)
        // Tour beat: the differentiator claim is "Leaf writes the tool", so on a
        // guided run the seed authors immediately instead of leaving a prefilled box.
        if (seedAutoSubmit) submit(seed)
      }
    }
  }, [seed, seedSignal])

  // Calm ticking wall-clock while authoring — real elapsed, no spinner.
  useEffect(() => {
    if (!busy) return
    startRef.current = Date.now()
    setElapsedMs(0)
    const id = setInterval(() => setElapsedMs(Date.now() - startRef.current), 250)
    return () => clearInterval(id)
  }, [busy])

  // `override` lets a caller submit a description that hasn't round-tripped
  // through state yet (the tour seed). Click handlers pass a MouseEvent, so only
  // a string is honoured — mirroring the existing seed/override pattern.
  async function submit(override) {
    const d = (typeof override === 'string' ? override : desc).trim()
    if (!d || !buildEntitled) return
    setBusy(true); setErr(null); setPublishErr(null); setGrantGate(false); setBuildGate(false); setSvcGate(false); setAuthored(null)
    try {
      const res = await onAuthor(d)
      setAuthored(res)
    } catch (e) {
      // Expected "not-an-alarm" rejections surface as calm gates, never red:
      //   entitlementRequired — the plan doesn't include authoring (build).
      //   grantRequired       — no Claude account linked to fund the agent.
      //   serviceDown (B2)    — the authoring agent was unreachable (502/503/504).
      if (e && e.entitlementRequired) setBuildGate(true)
      else if (e && e.grantRequired) setGrantGate(true)
      else if (isServiceDown(e)) setSvcGate(true)
      else setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  async function publish() {
    if (!authored || publishing) return
    setPublishing(true); setPublishErr(null)
    try {
      const published = await onPublish(authored)
      setAuthored(published)
    } catch (e) {
      setPublishErr(lifecycleMessage(e))
    } finally {
      setPublishing(false)
    }
  }

  // X1 mnemonic: R retries a failed Generate (never while typing in a field).
  useEffect(() => {
    if (!err || busy) return
    const onKey = (e) => {
      if (e.key !== 'r' && e.key !== 'R') return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const tag = (e.target?.tagName || '').toLowerCase()
      if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return
      e.preventDefault()
      submit()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  })

  const secs = Math.floor(elapsedMs / 1000)
  const prov = authored ? readProvenance(authored) : null
  const provLine = authored ? authoredProvLine(authored) : null

  return (
    <div className="author-panel author-inner">
      <p className="panel-sub">Describe a CAD tool in plain English. Leaf stages it for review before publication.</p>
      {(!buildEntitled || buildGate)
        ? <BuildGate />
        : svcGate
          ? <ServiceGate />
          : (notLinked || grantGate)
            ? <AuthorGate onLinkClaude={onLinkClaude} />
            : null}
      {/* F1 anatomy: 12px muted label above the field */}
      <label className="field-label" htmlFor="author-desc">What should the tool do?</label>
      <textarea
        id="author-desc"
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        placeholder="e.g. count panels within 24in of the roof edge"
        rows={3}
        disabled={busy}
      />
      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="chip" onClick={() => setDesc(ex)} disabled={busy}>{ex}</button>
        ))}
      </div>
      <button className="btn primary" disabled={busy || !desc.trim() || !buildEntitled} onClick={submit}>
        {busy ? 'Authoring…' : 'Generate tool'}
      </button>
      {busy && (
        <div className="authoring">
          Authoring with the agent · {secs}s
          <span className="dim"> · real authoring takes ~1–2 min · safe to wait</span>
        </div>
      )}
      {err && (
        <div className="inline-error">
          <span>Couldn’t author the tool — {err}</span>
          <button className="chip-act" onClick={submit}>Retry <span className="key">R</span></button>
          <span className="err-note">Your description is preserved.</span>
        </div>
      )}

      {authored && (
        <div className="authored" ref={authoredRef}>
          <div className="authored-head">
            <span className="tool-name">{authored.tool.name}</span>
            <ProvenanceStatus prov={prov} />
            <TelemetryChip telemetry={authored.telemetry} />
          </div>
          {provLine && <div className="prov-line">{provLine}</div>}
          {prov.scan.length > 0 && (
            <div className="scan-flags">
              {prov.scan.map((f) => (
                <span key={f} className="scan-flag">{f}</span>
              ))}
            </div>
          )}
          <p className="authored-desc">{authored.preview || authored.tool.description}</p>
          {!authored.published && (
            <div className="customization-state" role="status">
              <span className="dot square" aria-hidden="true" />
              <span>{authored.demo ? 'Demo preview staged. Publish to make it available in this demo.' : 'Staged and awaiting approval. It is not runnable until publication succeeds.'}</span>
            </div>
          )}
          {authored.published && authored.demo_session_only && (
            <div className="customization-state" role="status">
              <span className="dot square" aria-hidden="true" />
              <span>Published for this demo session only. Reloading restores the seeded demo catalog.</span>
            </div>
          )}
          {authored.validation && (
            <div className="customization-detail"><span>Server validation</span><pre>{typeof authored.validation === 'string' ? authored.validation : JSON.stringify(authored.validation, null, 2)}</pre></div>
          )}
          {(authored.diff_summary || authored.diff) && (
            <div className="customization-detail"><span>Server diff</span><pre>{typeof (authored.diff_summary || authored.diff) === 'string' ? (authored.diff_summary || authored.diff) : JSON.stringify(authored.diff_summary || authored.diff, null, 2)}</pre></div>
          )}
          <pre className="code"><code>{authored.code}</code></pre>
          {!authored.published ? (
            <button className="chip-act" onClick={publish} disabled={publishing}>
              {publishing ? 'Publishing…' : 'Publish tool'}
            </button>
          ) : (
            <button className="chip-act" onClick={() => onUseAuthored(authored.tool)}>
              Run it now
            </button>
          )}
          {publishErr && <div className="customization-state error" role="status">{publishErr}</div>}
        </div>
      )}

    </div>
  )
}
