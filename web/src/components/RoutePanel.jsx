// Renders the §12 nl-prompt routing decision as the calm confirm-before-run
// surface. Three lanes:
//   build -> a NEW capability: point at the (prefilled) author flow.
//   solve -> a cloud-solver job not wired into this demo: the design's honest
//            degraded pattern (no fake execution).
//   run   -> a matched catalog capability. confidence >= 0.7 => prefilled tool
//            + params + a single Run (the USER confirms — paid actions never
//            auto-execute). confidence < 0.7 => best guess + "did you mean"
//            alternatives.

function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined) out[k] = p.default
  }
  return out
}

function StubFlag({ route }) {
  if (!route.stub) return null
  return (
    <span className="stub-flag" title={route.stubReason || 'Routed by the local stub matcher'}>
      local router stub
    </span>
  )
}

function ParamRows({ params }) {
  const entries = Object.entries(params || {})
  if (entries.length === 0) return <div className="route-note">No parameters — runs as-is.</div>
  return (
    <div className="route-params">
      {entries.map(([k, v]) => (
        <div key={k} className="route-param">
          <span>{k.replace(/_/g, ' ')}</span>
          <span>{String(v)}</span>
        </div>
      ))}
    </div>
  )
}

function Alternatives({ alts, onPick }) {
  if (!alts || alts.length === 0) return null
  return (
    <div className="route-alts">
      {alts.map((a) => (
        <button key={a.tool} className="route-alt" onClick={() => onPick(a.tool)}>
          <span className="an">{a.tool}</span>
          <span className="ad">{a.description}</span>
        </button>
      ))}
    </div>
  )
}

export default function RoutePanel({ route, tools, running, onRun, onPickAlternative, onOpenAuthor }) {
  if (!route) return null

  // ---- BUILD lane -----------------------------------------------------------
  if (route.lane === 'build') {
    return (
      <div className="route build">
        <div className="route-head">
          <span className="route-lane build">build</span>
          <span className="route-tool">author a new capability</span>
          <span className="route-conf"><StubFlag route={route} /></span>
        </div>
        <div className="route-body">
          <p className="route-rationale">{route.rationale}</p>
          <div className="route-actions">
            <button className="btn primary" onClick={onOpenAuthor}>Open author flow</button>
            <span className="route-note">Your description is prefilled below in “Author a tool”.</span>
          </div>
        </div>
      </div>
    )
  }

  // ---- SOLVE lane -----------------------------------------------------------
  if (route.lane === 'solve') {
    return (
      <div className="route solve">
        <div className="route-head">
          <span className="route-lane solve">solve</span>
          <span className="route-tool">cloud solve lane</span>
          <span className="route-conf"><StubFlag route={route} /></span>
        </div>
        <div className="route-body">
          <p className="route-rationale">{route.rationale}</p>
          <div className="route-actions">
            <span className="route-note">
              Solve lane coming — string / combiner / route jobs run on the cloud solver, which
              is not connected in this demo. Nothing was executed.
            </span>
          </div>
        </div>
      </div>
    )
  }

  // ---- RUN lane -------------------------------------------------------------
  const toolObj = tools.find((t) => t.name === route.tool) || null
  const confident = route.confidence >= 0.7
  const params = toolObj ? { ...defaultsOf(toolObj.params), ...(route.params || {}) } : (route.params || {})
  const isWrite = (toolObj?.capabilities || []).includes('drawing.write')

  return (
    <div className={`route ${confident ? '' : 'lowconf'}`}>
      <div className="route-head">
        <span className="route-lane">run</span>
        <span className="route-tool">{route.tool || 'no match'}</span>
        {isWrite && <span className="cap write">drawing.write</span>}
        <span className={`route-conf ${confident ? '' : 'low'}`}>
          confidence <b>{route.confidence.toFixed(2)}</b> <StubFlag route={route} />
        </span>
      </div>
      <div className="route-body">
        <p className="route-rationale">{route.rationale}</p>

        {confident ? (
          <>
            {toolObj ? (
              <>
                <ParamRows params={params} />
                <div className="route-actions">
                  <button className="btn primary" disabled={running} onClick={() => onRun(toolObj, params)}>
                    {running ? 'Running' : (isWrite ? 'Run (creates a new version)' : 'Run')}
                  </button>
                  <span className="route-note">You confirm before anything runs.</span>
                </div>
              </>
            ) : (
              <div className="route-actions">
                <span className="route-note">
                  “{route.tool}” is a live-mode capability — it is not in the mock catalog. Switch off
                  mock mode to run it, or pick one of these:
                </span>
                <Alternatives alts={route.alternatives} onPick={onPickAlternative} />
              </div>
            )}
          </>
        ) : (
          <>
            {toolObj && (
              <div className="route-actions" style={{ marginBottom: 10 }}>
                <button className="btn primary" disabled={running} onClick={() => onRun(toolObj, params)}>
                  Run best guess: {route.tool}
                </button>
              </div>
            )}
            <div className="route-note">Did you mean:</div>
            <Alternatives alts={route.alternatives} onPick={onPickAlternative} />
          </>
        )}
      </div>
    </div>
  )
}
