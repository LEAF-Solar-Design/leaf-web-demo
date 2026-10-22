import { Handle, Position } from '@xyflow/react'
import { laneLabel } from './elementCatalog.js'
import { Measurement } from './LatencyPipe.jsx'

export default function NodeCard({ data, selected }) {
  const { manifest, missing = [], settingsError, measurement, pending, handleAllowed } = data
  return <article className={`lab-node-card${selected ? ' is-selected' : ''}`} aria-label={manifest.title}>
    <header><span className={`lab-lane-chip lane-${manifest.lane}`}>{laneLabel(manifest.lane)}</span>
      <span className="lab-effect-chip" aria-label={`Effect: ${manifest.effect}`}>{manifest.effect}</span>
      <h3>{manifest.title}</h3></header>
    <p role="status">{missing.length ? `Not ready: pipe required for ${missing.join(', ')}` : settingsError ? `Not ready: ${settingsError}` : 'Ready'}</p>
    <Measurement measurement={measurement} budget={manifest.latency_budget_us} />
    {['inputs', 'outputs'].map(direction => <div key={direction} className="lab-node-ports">
      {Object.entries(manifest[direction]).map(([name, pin], index, pins) => {
        const status = pending ? handleAllowed(direction, name) : { ok: true, reason: '' }
        const allowed = status.ok
        return <div className="lab-node-pin" key={name} title={status.reason || undefined}>
          <Handle id={name} type={direction === 'inputs' ? 'target' : 'source'}
            position={direction === 'inputs' ? Position.Top : Position.Bottom}
            style={{ left: `${(index + 1) * 100 / (pins.length + 1)}%` }}
            isConnectable={allowed} aria-label={`${manifest.title} ${direction} ${name}`} title={status.reason || undefined} />
          <span>{direction === 'inputs' ? 'In' : 'Out'}: {name} · {pin.mode} · {pin.unit}
            {pin.required && !pin.has_default ? ' · required' : ''}
            {pin.has_default ? ` · default ${JSON.stringify(pin.default)}` : ''}</span>
        </div>
      })}
    </div>)}
  </article>
}
