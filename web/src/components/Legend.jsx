// Layer legend overlaid on the viewer — color swatch, count, visibility toggle.
export default function Legend({ layers, counts, colorForLayer, visibleLayers, onToggle }) {
  return (
    <div className="legend">
      <div className="legend-title">Layers</div>
      {layers.map((l) => {
        const n = counts[l] || 0
        const on = visibleLayers[l] !== false
        return (
          <button
            key={l}
            className={`legend-row ${on ? '' : 'off'} ${n === 0 ? 'empty' : ''}`}
            onClick={() => onToggle(l)}
            disabled={n === 0}
            title={n === 0 ? 'no geometry on this layer' : 'toggle visibility'}
          >
            <span className="swatch" style={{ background: colorForLayer(l), opacity: on ? 1 : 0.25 }} />
            <span className="legend-name">{l}</span>
            <span className="legend-count">{n.toLocaleString()}</span>
          </button>
        )
      })}
    </div>
  )
}
