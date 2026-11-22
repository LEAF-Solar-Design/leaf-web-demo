import { WidgetText } from './WidgetText.jsx'
import { renderWidget } from './widgetRender.js'

const paint = token => `var(${token}, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))`

export function WidgetCard({ doc, format, board, renderer, label = 'Widget' }) {
  const tree = renderWidget(doc, { format, board, renderer })
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${tree.width} ${tree.height}`} style={{ width: '100%', overflow: 'hidden', borderRadius: tree.radius, background: tree.background ? `var(${tree.background})` : undefined, fontFamily: 'var(--leaf-font-sans)' }}>
      {tree.nodes.map((n) => (
        <svg key={n.id} x={n.x} y={n.y} width={n.w} height={n.h} viewBox={n.viewBox ?? `0 0 ${n.w} ${n.h}`} opacity={n.opacity} overflow="hidden">
          {n.kind === 'text' ? <WidgetText node={n} fill={paint(n.color)} />
            : n.kind === 'box' ? <rect width={n.w} height={n.h} fill={paint(n.color)} />
              : <path d={n.path} fill={n.stroke ? 'none' : paint(n.color)} stroke={n.stroke ? paint(n.color) : undefined} strokeWidth={n.stroke ? 2 * n.scale : undefined} />}
        </svg>
      ))}
    </svg>
  )
}
export default WidgetCard
