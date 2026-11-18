export function WidgetText({ node, fill }) {
  const { align, w, h, text = '', fontSize, weight } = node
  const anchor = { left: 'start', center: 'middle', right: 'end' }[align]
  const x = align === 'right' ? w : align === 'center' ? w / 2 : 0
  const parts = text.split(/(\r\n|[\r\n])/)
  const lineCount = (parts.length + 1) / 2
  const rowHeight = h / lineCount
  return (
    <text x={x} y={h / 2} dominantBaseline="central" textAnchor={anchor}
      fill={fill} fontSize={fontSize / lineCount} fontWeight={weight}>
      {lineCount === 1 ? text : parts.map((part, index) => index % 2 === 1 ? part : (
        <tspan key={index} x={x} y={(index / 2 + 0.5) * rowHeight}>{part}</tspan>
      ))}
    </text>
  )
}
