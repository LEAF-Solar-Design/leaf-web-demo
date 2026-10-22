import { renderWidget } from './widgetRender.js'

export function WidgetCard({ doc, format, board, renderer, label = 'Widget' }) {
  const tree = renderWidget(doc, { format, board, renderer })
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${tree.width} ${tree.height}`} style={{ width: '100%', overflow: 'hidden', borderRadius: tree.radius, background: tree.background ? `var(${tree.background})` : undefined, fontFamily: 'var(--leaf-font-sans)' }}>
      {tree.nodes.map((n) => (
        <svg key={n.id} x={n.x} y={n.y} width={n.w} height={n.h} viewBox={n.viewBox ?? `0 0 ${n.w} ${n.h}`} opacity={n.opacity} overflow="hidden">
          {n.kind === 'text' ? <text x={n.align === 'right' ? n.w : n.align === 'center' ? n.w / 2 : 0} y={n.h / 2} dominantBaseline="central" textAnchor={n.align === 'right' ? 'end' : n.align === 'center' ? 'middle' : 'start'} fill={`var(${n.color})`} fontSize={n.fontSize} fontWeight={n.weight}>{n.text}</text>
            : n.kind === 'box' ? <rect width={n.w} height={n.h} fill={`var(${n.color})`} />
              : <path d={n.path} fill={n.stroke ? 'none' : `var(${n.color})`} stroke={n.stroke ? `var(${n.color})` : undefined} strokeWidth={n.stroke ? 2 * n.scale : undefined} />}
        </svg>
      ))}
    </svg>
  )
}
export default WidgetCard
