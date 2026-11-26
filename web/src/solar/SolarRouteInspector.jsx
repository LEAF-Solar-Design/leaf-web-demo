import { useMemo, useState } from 'react'

const routeNames = {
  'start homerun': 'Start home run',
  'end homerun': 'End home run',
  feeder: 'Feeder',
}

export default function SolarRouteInspector({ overlay }) {
  const polylines = overlay?.polylines
  const drawingKey = overlay?.drawingKey
  const routes = useMemo(
    () => polylines?.filter(line => Object.hasOwn(line, 'route')) ?? [],
    [polylines],
  )
  const [selection, setSelection] = useState({ polylines, drawingKey, id: '', index: 0 })
  if (selection.polylines !== polylines || selection.drawingKey !== drawingKey) {
    setSelection({ polylines, drawingKey, id: '', index: 0 })
    return null
  }
  if (!routes.length) return null

  const activeRoute = routes.find(line => line.route.id === selection.id)
  const index = selection.index
  const point = activeRoute?.pts[index]
  const move = offset => setSelection(current => ({ ...current, index: current.index + offset }))

  return (
    <details>
      <summary>Cable routes</summary>
      <label>
        Route{' '}
        <select value={selection.id} onChange={event => {
          setSelection({ polylines, drawingKey, id: event.target.value, index: 0 })
        }}>
          <option value="">Choose a route</option>
          {routes.map(({ route }, i) => (
            <option key={route.id} value={route.id}>{routeNames[route.kind]} {i + 1}</option>
          ))}
        </select>
      </label>
      {point && (
        <>
          <p role="status">Point {index + 1} of {activeRoute.pts.length} · X {point[0]}, Y {point[1]} (drawing units)</p>
          <button type="button" disabled={index === 0} onClick={() => move(-1)}>Previous point</button>{' '}
          <button type="button" disabled={index === activeRoute.pts.length - 1} onClick={() => move(1)}>Next point</button>
        </>
      )}
    </details>
  )
}
