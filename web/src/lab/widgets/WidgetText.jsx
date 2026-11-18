export function WidgetText({ node, fill }) {
  const { align, w, h, text = '', fontSize, weight } = node
  const anchor = { left: 'start', center: 'middle', right: 'end' }[align]
  const x = align === 'right' ? w : align === 'center' ? w / 2 : 0
  const lines = text.split(/\r\n|[\r\n]/)
  const rowHeight = h / lines.length
  return (
    <text x={x} y={h / 2} dominantBaseline="central" textAnchor={anchor}
      fill={fill} fontSize={fontSize / lines.length} fontWeight={weight}>
      {lines.length === 1 ? text : lines.map((line, index) => (
        <tspan key={index} x={x} y={(index + 0.5) * rowHeight}>{line}</tspan>
      ))}
    </text>
  )
}
