import { CANVAS_HEIGHT, CANVAS_WIDTH, validateElements } from './widgetDoc.js'
import './widgets.css'

const colors = { ink: '#ffffff', slate: '#a1a1aa', leaf: '#86efac', loss: '#fda4af' }

export function WidgetPreview({ elements }) {
  const validated = validateElements(elements)
  return (
    <section className="widget-preview" aria-label="Preview">
      <h3>Preview</h3>
      <p>Lock screen · 361 × 160 points</p>
      <div className="widget-preview-canvas">
        {validated.filter((element) => element.visible).map((element) => (
          <div key={element.id} data-element-id={element.id} className="widget-preview-text" style={{
            left: `${element.x / CANVAS_WIDTH * 100}%`, top: `${element.y / CANVAS_HEIGHT * 100}%`,
            width: `${element.w / CANVAS_WIDTH * 100}%`, height: `${element.h / CANVAS_HEIGHT * 100}%`,
            fontSize: `${element.h * 0.8 * element.scale / CANVAS_WIDTH * 100}cqw`,
            fontWeight: element.weight, color: colors[element.color], textAlign: element.align,
          }}>{element.text}</div>
        ))}
      </div>
    </section>
  )
}

export default WidgetPreview
