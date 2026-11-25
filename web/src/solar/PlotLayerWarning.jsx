import React, { useMemo } from 'react'

import { plotLayerNames } from './plotLayerNames.js'

export function PlotLayerWarning({ intake }) {
  const affected = useMemo(() => plotLayerNames(intake), [intake])
  if (!affected.length) return null
  return <details>
    <summary>Plot warning: {affected.length} affected {affected.length === 1 ? 'layer' : 'layers'}</summary>
    <p>Before exporting, turn on, thaw and enable plotting for any listed layer you want on the sheet.</p>
    <ul>{affected.map(name => <li key={name}>{name}</li>)}</ul>
  </details>
}
