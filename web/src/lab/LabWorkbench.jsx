import { useEffect, useState } from 'react'
import { fetchLabStatus } from './labRoute.js'
import { registerLabViews } from './views.js'
import './lab.css'

const dormantMessages = {
  off: 'Lab is turned off.',
  'auth-off': 'Lab sign-in service is unavailable.',
  'signed-out': 'Sign in to use Lab.',
  unavailable: 'Lab is unavailable right now.',
}

export function LabWorkbench({ route, hidden = false, status }) {
  const views = registerLabViews()
  const [liveStatus, setLiveStatus] = useState(null)
  useEffect(() => {
    if (status !== undefined) return undefined
    let current = true
    fetchLabStatus().then((next) => { if (current) setLiveStatus(next) })
    return () => { current = false }
  }, [status])
  const state = (status === undefined ? liveStatus : status)?.state || 'unavailable'
  const available = state === 'on'
  const activeView = views.some(({ id }) => id === route?.view) ? route.view : 'graph'

  return (
    <section className="lab-workbench" aria-label="Lab" hidden={hidden}>
      <h1>Lab</h1>
      {!available && <p role="status">{dormantMessages[state] || dormantMessages.unavailable}</p>}
      <nav aria-label="Lab views" hidden={!available}>
        {views.map(({ id, label, path }) => (
          <a key={id} href={path} aria-current={activeView === id ? 'page' : undefined}>{label}</a>
        ))}
      </nav>
      {views.map(({ id, label, component: View }) => {
        const inactive = hidden || !available || activeView !== id
        return (
          <div key={id} data-lab-view={id} role="region" aria-label={`${label} view`} hidden={inactive}>
            <View hidden={inactive} />
          </div>
        )
      })}
    </section>
  )
}

export default LabWorkbench
