import { readController } from './controller.js'

const shown = value => value ?? 'Not reported'
const money = value => value === null || value === undefined ? 'Not reported'
  : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value))
const windowText = value => value && typeof value === 'object' ? `${shown(value.start)}..${shown(value.end)}` : shown(value)

export default function TestsKeepersRail({ provider, controller = readController(provider), collapsed = false, now = Date.now() }) {
  const latest = controller?.versions.latest
  const hours = latest?.live_at ? Math.max(0, (now - Date.parse(latest.live_at)) / 3600000).toFixed(1) : null
  return <aside aria-label="Tests and Keepers">
    <p>Report only. Keep, Undo and combine run in the champion rule controller, not here.</p>
    <details open={!collapsed}>
      <summary>Tests / Keepers</summary>
      {!controller ? <p>No controller report yet</p> : <>
        {controller.stale && <p>stale</p>}
        {controller.truncated && <p>truncated</p>}
        <section aria-label="Versions"><h3>Versions</h3>
          <p>Champion {controller.versions.champion_digest?.slice(0, 12) ?? 'Not reported'}</p>
          {latest ? <>
            <p>Latest {latest.rollout_id} · {shown(latest.kind)} · {shown(latest.feature)} · {latest.state}</p>
            <p>Live at {shown(latest.live_at)}{hours !== null && ` · ${hours} hours elapsed`}</p>
            <p>Closed at {shown(latest.closed_at)}</p>
            {latest.rollback_pending ? <p>Restart pending</p>
              : latest.closed_at && (['rollback', 'rolled_back', 'undone'].includes(latest.state.toLowerCase())
                || (latest.kind === 'rollback' && latest.state.toLowerCase() === 'closed')) && <p>Undone</p>}
            {latest.source_pending && <p>Source pending</p>}
          </> : <p>No latest rollout</p>}
        </section>
        <section aria-label="Tests"><h3>Tests</h3>
          {!controller.screen_trials.length && <p>No Tests reported</p>}
          <ul>{controller.screen_trials.map((trial, index) => <li key={`${trial.rollout_id}-${index}`}>
            {trial.rollout_id} · {trial.state} · {shown(trial.at)}
            {trial.screen ? <>
              <p>Verdict {shown(trial.screen.verdict)} · {shown(trial.screen.reason)}</p>
              <p>Net after fees {money(trial.screen.net_dollars)} · Episodes {shown(trial.screen.episodes_completed)} · Scopes {shown(trial.screen.scope_count)} · Kind {shown(trial.screen.kind)} · {trial.screen.complete === true ? 'complete' : trial.screen.complete === false ? 'incomplete' : 'Completeness not reported'}</p>
            </> : <p>Screen not reported</p>}
          </li>)}</ul>
        </section>
        <section aria-label="Keepers"><h3>Keepers</h3>
          {controller.keepers_unavailable ? <p>Keepers unavailable</p> : <>
            {!controller.keepers.length && <p>No Keepers reported</p>}
            <ul>{controller.keepers.map((keeper, index) => <li key={`${keeper.rollout_id}-${index}`}>
              <p>Reported, not active: ratification needed</p>
              <p>{keeper.feature} · Net after fees {money(keeper.net_dollars)} · Window {windowText(keeper.window)} · Episodes {shown(keeper.episodes_completed)} · Scopes {shown(keeper.scope_count)} · Added {shown(keeper.added_at)}</p>
            </li>)}</ul>
          </>}
        </section>
      </>}
    </details>
  </aside>
}
