import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@xyflow/react'

export function Measurement({ measurement, budget }) {
  const measured = measurement && Number.isFinite(measurement.p50_us) && Number.isFinite(measurement.p99_us)
  const over = measured && budget && (measurement.p50_us > budget.p50 || measurement.p99_us > budget.p99)
  const hasValue = measurement && Object.hasOwn(measurement, 'value')
  const value = hasValue ? JSON.stringify(measurement.value) : 'not measured'
  return <div className="lab-measurement" data-budget-state={measured ? over ? 'over-budget' : 'within-budget' : 'not-measured'}>
    <div title={value}>Value {value?.slice(0, 100)} · Count {measurement?.count ?? 'not measured'}</div>
    <div>p50 {measured ? `${measurement.p50_us / 1000} ms` : 'not measured'} · p99 {measured ? `${measurement.p99_us / 1000} ms` : 'not measured'}</div>
    <div>Budget p50 {budget ? `${budget.p50 / 1000} ms` : 'not measured'} · p99 {budget ? `${budget.p99 / 1000} ms` : 'not measured'} · {measured ? over ? 'over budget' : 'within budget' : 'not measured'}</div>
  </div>
}

export default function LatencyPipe({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected }) {
  const [path, x, y] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })
  return <>
    <BaseEdge id={id} path={path} style={{ stroke: 'var(--leaf-color-primary)', strokeWidth: selected ? 3 : 2 }} />
    <EdgeLabelRenderer><div className="lab-pipe-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}>
      <Measurement measurement={data?.measurement} budget={data?.budget} />
      <button type="button" aria-label={`Disconnect ${id}`} onClick={() => data?.disconnect(id)}>Disconnect</button>
    </div></EdgeLabelRenderer>
  </>
}
