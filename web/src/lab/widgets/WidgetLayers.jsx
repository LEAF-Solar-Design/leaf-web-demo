import { PRESETS } from './widgetCatalog.js'

export default function WidgetLayers({ elements, selectedId, dispatch }) {
  return <aside aria-label="Layers">
    <h3>Layers</h3>
    <ul className="widgets-element-list" aria-label="Text elements">
      {elements.map((element, index) => {
        const name = element.preset === 'text' ? `Text ${index + 1}: ${element.text || '(empty)'}` : `${PRESETS[element.preset]?.n ?? element.kind} ${element.id}`
        return <li key={element.id} className="widget-layer">
          <button type="button" data-layer-id={element.id} aria-pressed={selectedId === element.id} onClick={() => dispatch({ type: 'select', id: element.id, reveal: true })}
            onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); dispatch({ type: 'select', id: element.id, reveal: true }) } }}>{name}</button>
          <div className="widget-layer-actions">
            <button type="button" aria-label={`${element.visible ? 'Hide' : 'Show'} ${element.id}`} onClick={() => dispatch({ type: 'visibility', id: element.id })}>{element.visible ? 'Hide' : 'Show'}</button>
            <button type="button" aria-label={`Move ${element.id} earlier`} disabled={index === 0} onClick={() => dispatch({ type: 'reorder', id: element.id, index: index - 1 })}>↑</button>
            <button type="button" aria-label={`Move ${element.id} later`} disabled={index === elements.length - 1} onClick={() => dispatch({ type: 'reorder', id: element.id, index: index + 1 })}>↓</button>
            <button type="button" aria-label={`Remove ${element.id}`} onClick={() => dispatch({ type: 'delete', id: element.id })}>×</button>
          </div>
        </li>
      })}
    </ul>
  </aside>
}
