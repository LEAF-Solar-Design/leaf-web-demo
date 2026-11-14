import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { createTextElement } from './widgetDoc.js'
import { createWidgetDoc } from './useWidgetEditor.js'
import WidgetCard from './WidgetCard.jsx'

afterEach(cleanup)
const text = id => ({ ...createTextElement(id), min_renderer: '1.0.0' })
const graphic = id => { const { text: _text, ...element } = text(id); return element }

describe('WidgetCard host palette compatibility', () => {
  it('keeps persisted default slate text readable when the host lacks its data-series token', () => {
    const doc = createWidgetDoc()
    doc.formats.la.els = [{ ...text('text1'), text: 'Sample portfolio: 1010' }]
    const original = JSON.stringify(doc)
    render(<WidgetCard doc={doc} />)
    const preview = screen.getByRole('img', { name: 'Widget' })
    expect(preview).toHaveAttribute('viewBox', '0 0 361 160')
    expect(screen.getByText('Sample portfolio: 1010')).toHaveAttribute('fill',
      'var(--leaf-color-data-series-5, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))')
    expect(JSON.stringify(doc)).toBe(original)
  })

  it('retains explicit palette roles for text and graphics ahead of the fallback', () => {
    const doc = createWidgetDoc()
    doc.formats.la.els = [
      { ...text('text1'), color: 'leaf' },
      { ...graphic('box1'), kind: 'box', color: 'loss' },
      { ...graphic('icon1'), kind: 'svg-path', preset: 'leafIcon', color: 'slate' },
      { ...graphic('ring1'), kind: 'svg-path', preset: 'ring', color: 'leaf' },
    ]
    const { container } = render(<WidgetCard doc={doc} />)
    expect(screen.getByText('Your text')).toHaveAttribute('fill',
      'var(--leaf-color-success, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))')
    expect(container.querySelector('rect')).toHaveAttribute('fill',
      'var(--leaf-color-data-series-3, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))')
    expect(container.querySelector('path[fill]:not([fill="none"])')).toHaveAttribute('fill',
      'var(--leaf-color-data-series-5, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))')
    const stroke = container.querySelector('path[stroke]')
    expect(stroke).toHaveAttribute('fill', 'none')
    expect(stroke).toHaveAttribute('stroke',
      'var(--leaf-color-success, var(--leaf-color-foreground, var(--leaf-ink, currentColor)))')
  })
})
