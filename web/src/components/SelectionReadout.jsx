// Calm, token-styled readout of the entity the user picked in the viewport.
// Driven purely by user picking state (not by a Result envelope). Shows the
// DWG handle, entity kind, and layer, with a plain-text deselect affordance.
// No chip badges, no emoji, no animated loader — colors come from the calm
// --cv-* CSS variables (see styles.css).

const KIND_LABEL = {
  polyline: 'Polyline',
  insert: 'Block insert',
  '3dface': '3D face',
  entity: 'Entity',
}

export default function SelectionReadout({ selection, onDeselect }) {
  if (!selection) {
    return (
      <div className="selection-readout empty">
        <span className="sel-hint">Click an entity to select it</span>
      </div>
    )
  }

  const kind = KIND_LABEL[selection.kind] || 'Entity'

  return (
    <div className="selection-readout">
      <div className="sel-head">
        <span className="sel-kind">{kind}</span>
        <button type="button" className="sel-clear" onClick={onDeselect}>
          Deselect
        </button>
      </div>
      <dl className="sel-fields">
        <div className="sel-field">
          <dt>Handle</dt>
          <dd className="sel-handle">{selection.handle}</dd>
        </div>
        <div className="sel-field">
          <dt>Layer</dt>
          <dd>{selection.layer || '—'}</dd>
        </div>
        {selection.name && (
          <div className="sel-field">
            <dt>Block</dt>
            <dd>{selection.name}</dd>
          </div>
        )}
      </dl>
    </div>
  )
}
