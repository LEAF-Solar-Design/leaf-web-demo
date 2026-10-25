export const CHECKS = [
  ['builds', 'Builds'],
  ['safety', 'No unsafe code; a panic becomes an error, never a crash'],
  ['contract', "Matches the block's inputs and outputs"],
  ['tests', 'Tests from sample data'],
  ['speed', 'Speed'],
  ['corpus', 'Same answers as the live version on the last 4 hours of trades'],
]

export function checkStatus(check) {
  return ['pass', 'fail'].includes(check?.status) ? check.status : 'pending'
}

export default function BlockChecks({ checks }) {
  return <section aria-label="Checks before it can be used">
    <h4>Checks before it can be used</h4>
    <ol>{CHECKS.map(([key, label]) => {
      const check = checks?.[key]
      return <li key={key} data-check={key} data-status={checkStatus(check)}>
        <span>{label}</span>: <strong>{checkStatus(check)}</strong>
        {check?.detail && <p>{check.detail}</p>}
        {key === 'tests' && check?.passed != null && check?.total != null && <p>{check.passed} of {check.total} passed</p>}
        {key === 'speed' && <p>p50 {check?.p50_us ?? 'not measured'} µs; p99 {check?.p99_us ?? 'not measured'} µs
          {check?.budget && <>; budget p50 {check.budget.p50} µs, p99 {check.budget.p99} µs</>}</p>}
        {key === 'corpus' && check?.difference_count != null && <p>{check.difference_count} differ</p>}
        {key === 'corpus' && check?.differences?.length > 0 && <ul aria-label="First differences">
          {check.differences.map((difference, index) => <li key={index}><pre>{typeof difference === 'string' ? difference : JSON.stringify(difference, null, 2)}</pre></li>)}
        </ul>}
      </li>
    })}</ol>
  </section>
}
