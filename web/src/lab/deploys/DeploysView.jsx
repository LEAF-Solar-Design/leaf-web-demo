import { useEffect, useState } from 'react'
import { labArtifactsClient } from '../history/labArtifactsClient.js'

const cardStyle = {
  background: 'var(--leaf-color-surface)', color: 'var(--leaf-color-foreground)',
  border: '1px solid var(--leaf-color-border)', padding: 'var(--leaf-spacing-panel-padding)',
  borderRadius: 'var(--leaf-radius-panel)', fontFamily: 'var(--leaf-font-sans)',
}
const receiptPath = '~/.claude/program/release-train/receipts/<surface>/<surface>__<candidate>__<utc>__<to_state>.json'

export default function DeploysView({ hidden = false, client = labArtifactsClient }) {
  const [, refresh] = useState(0)
  useEffect(() => {
    if (hidden) return undefined
    const unsubscribe = client.subscribe(() => refresh(value => value + 1))
    refresh(value => value + 1)
    return unsubscribe
  }, [client, hidden])
  if (hidden) return null

  const { head, published, lastSaved } = client.state
  const publication = published == null ? 'Unpublished' : `Published r${published}`
  const receipt = published != null && lastSaved?.revision === published && lastSaved?.receipt_id
    ? lastSaved.receipt_id : 'Publish receipt id unavailable in the current ledger client'
  const cards = [
    { title: 'Revision publish', trigger: 'Publish in the save bar',
      rail: <>Tenant ledger. {head == null ? 'No saved head' : `Head r${head}`}. {publication}.</>,
      receipt: <>{receipt}. {lastSaved == null ? 'No saved revision' : `Last saved r${lastSaved.revision}`}.</>,
      rollback: 'Restore the prior revision as a new revision' },
    { title: 'Read-only activation', trigger: 'Request read-only activation', rail: `Tenant ledger. ${publication}`,
      receipt: 'Activation evidence arrives with SEL-312', rollback: 'Handoff to SEL-312 for activation rollback' },
    { title: 'Widget publish', trigger: 'Request widget publication', rail: `Tenant ledger. ${publication}`,
      receipt: 'Widget publication arrives with SEL-309', rollback: 'Handoff to SEL-309 for widget rollback' },
    { title: 'Studio release', trigger: 'Approved Studio release',
      rail: <>Studio staging: native ECS, forward-normal, staging alias (observe_only). Platform production: leaf-platform-production (operator_yes) through release-train promote. Never deploy production with deploy-web.py without <code>--preview</code>.</>,
      receipt: 'Release-train receipt for the target surface', rollback: 'release-train rollback' },
    { title: 'Ops UI and API release', trigger: 'Release the ops UI or API separately',
      rail: <><span style={{ display: 'block' }}>ops-dashboard: Vercel preview to production alias (observe_only).</span><span style={{ display: 'block' }}>kashbot-api: ECR image to ECS task definition; standing authorization under OPERATOR-WISHES, recorded in the registry.</span></>,
      receipt: 'Separate release-train receipts for ops-dashboard and kashbot-api', rollback: 'release-train rollback for the affected surface' },
    { title: 'Runner release', trigger: 'Approved rollout', rail: 'Runner release-train surface',
      receipt: 'Runner release-train LIVE receipt', rollback: 'Return to the previous LIVE revision' },
    { title: 'Trading lifecycle', trigger: 'Requested action: recorded separately from its outcome', rail: 'Runner outcome: reported by the runner',
      receipt: 'Observed revision: recorded separately from the requested action and runner outcome', rollback: 'Request a compensating action and observe the runner outcome' },
    { title: 'Native renderer', trigger: 'Approved internal TestFlight release', rail: 'Internal TestFlight; production exposure is separate',
      receipt: 'Internal TestFlight receipt', rollback: 'Return to the prior internal build; review production exposure separately' },
  ]
  return <section aria-label="Deploys">
    <h2>Deploys</h2>
    <ol>
      {cards.map((card, index) => <li key={card.title}>
        <article data-kind={index + 1} data-live={index < 3 ? 'true' : 'false'} style={cardStyle}>
          <h3>{card.title}</h3>
          {index >= 3 && <span className="lab-effect-chip">Handoff</span>}
          <dl>
            {['Trigger', 'Rail', 'Receipt', 'Rollback'].map(label => <div key={label}>
              <dt>{label}</dt><dd>{card[label.toLowerCase()]}</dd>
            </div>)}
          </dl>
        </article>
      </li>)}
    </ol>
    <section aria-label="Deploy state legend">
      <h3>State legend</h3>
      <dl>
        <dt>requested</dt><dd>AUTHORIZED pending; authorization does not prove activation.</dd>
        <dt>published</dt><dd>Tenant ledger publish receipt; no running state is implied.</dd>
        <dt>running</dt><dd>PROMOTING with activation evidence.</dd>
        <dt>observed</dt><dd>LIVE receipt.</dd>
      </dl>
      <p>Release-train progression: BUILT, PROVEN, ELIGIBLE, AUTHORIZED, PROMOTING, LIVE.</p>
      <p>Release-train receipt path: <code>{receiptPath}</code></p>
    </section>
    <p><code>migrate-leaf-platform-production-db.yml</code> runs before any migration-bearing image, even when the Lab rail is off.</p>
  </section>
}
