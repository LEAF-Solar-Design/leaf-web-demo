import { nsToMs } from './evidence.js'

export default function LatencyWaterfall({ receipt }) {
  const segments = receipt?.segments
  if (!segments) return <p>Timing not measured</p>
  const total = segments.reduce((sum, segment) => sum + (segment.measurement === 'measured' ? nsToMs(segment.duration_ns) ?? 0 : 0), 0)
  return <ol aria-label="Bot-stamped latency waterfall">
    {segments.map((segment, index) => {
      const duration = segment.measurement === 'measured' ? nsToMs(segment.duration_ns) : null
      return <li key={index}>
        <span>{segment.from} → {segment.to}: {duration === null ? 'not measured' : `${duration} ms`}</span>
        {duration !== null && <div data-waterfall-bar style={{ width: `${total ? duration / total * 100 : 0}%`, height: 8, background: 'var(--leaf-color-primary)' }} />}
      </li>
    })}
  </ol>
}
