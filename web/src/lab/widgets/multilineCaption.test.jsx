import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import WidgetCard from './WidgetCard.jsx'
import WidgetPreview from './WidgetPreview.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(cleanup)

it.each([
  ['left', 'Start\nFinish', ['Start', 'Finish'], 'start', 0],
  ['center', 'Δοκιμή\r\n東京', ['Δοκιμή', '東京'], 'middle', 60],
  ['right', 'First\n\nLast', ['First', '', 'Last'], 'end', 120],
  ['left', 'One line', ['One line'], 'start', 0],
])('lays out %s caption %j in both widget views', (align, text, lines, anchor, x) => {
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('caption'), min_renderer: doc.min_renderer, align, text, h: 36 }]
  for (const View of [WidgetCard, WidgetPreview]) {
    const view = render(<View doc={doc} format="la" />)
    const label = view.container.querySelector('svg text')
    expect(label.parentElement).toHaveAttribute('overflow', 'hidden')
    expect(label).toHaveAttribute('text-anchor', anchor)
    expect(label).toHaveAttribute('x', String(x))
    expect(label).toHaveAttribute('y', '18')
    expect(Number(label.getAttribute('font-size'))).toBeCloseTo(28.8 / lines.length)
    expect(label.textContent).toBe(text)
    const spans = Array.from(label.querySelectorAll('tspan'))
    if (lines.length === 1) {
      expect(spans).toHaveLength(0)
    } else {
      expect(spans.map(span => span.textContent)).toEqual(lines)
      spans.forEach((span, i) => {
        expect(span).toHaveAttribute('x', String(x))
        expect(span).toHaveAttribute('y', String((i + 0.5) * 36 / lines.length))
      })
    }
    view.unmount()
  }
})
