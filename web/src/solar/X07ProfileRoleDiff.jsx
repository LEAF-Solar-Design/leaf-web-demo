import { trackerFields, trackerValues, validTrackers } from './x07TrackerRoles.js'
const names = ['module', 'obstruction', 'roof', 'trackers']
const object = value => value && typeof value === 'object' && !Array.isArray(value)
const valid = roles => object(roles) && Object.keys(roles).every(name => name === 'trackers' ? validTrackers(roles[name]) : names.includes(name)
  && object(roles[name]) && Object.keys(roles[name]).every(field => ['block', 'layer'].includes(field)
    && typeof roles[name][field] === 'string' && roles[name][field].length <= 4096))
const copy = role => Array.isArray(role) ? role.map(row => ({ ...row })) : Object.fromEntries(['block', 'layer'].filter(key => Object.hasOwn(role, key)).map(key => [key, role[key]]))
const same = (a, b) => Array.isArray(a) ? JSON.stringify(trackerValues(a)) === JSON.stringify(trackerValues(b)) : ['block', 'layer'].every(key => Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key])

export function compareRoleMaps(before, after) {
  if (!valid(before) || !valid(after)) return null
  const result = { added: {}, removed: {}, changed: {} }
  for (const name of names) {
    if (!Object.hasOwn(before, name) && Object.hasOwn(after, name)) result.added[name] = copy(after[name])
    else if (Object.hasOwn(before, name) && !Object.hasOwn(after, name)) result.removed[name] = copy(before[name])
    else if (Object.hasOwn(before, name) && !same(before[name], after[name])) result.changed[name] = { before: copy(before[name]), after: copy(after[name]) }
  }
  return result
}

function Values({ role }) {
  if (Array.isArray(role)) return <ul>{role.map(row => <li key={JSON.stringify([row.block, row.layer])}>
    {trackerFields.map(key => row[key]).join(' / ')}
  </li>)}</ul>
  return <dl>{Object.entries(role).map(([field, value]) => <div key={field}>
    <dt>{field === 'block' ? 'Block' : 'Layer'}</dt><dd><code>{JSON.stringify(value)}</code></dd>
  </div>)}</dl>
}

export default function X07ProfileRoleDiff({ beforeRoles, afterRoles, label = 'Role changes' }) {
  const diff = compareRoleMaps(beforeRoles, afterRoles)
  if (!diff) return <section aria-label={label}><p role="status">Role changes are unavailable.</p></section>
  const counts = ['added', 'removed', 'changed'].map(kind => Object.keys(diff[kind]).length)
  return <section aria-label={label}>
    <h4>{label}</h4>
    <p role="status">{counts.some(Boolean) ? `${counts[0]} added, ${counts[1]} removed, ${counts[2]} changed.` : 'No role changes.'}</p>
    {['added', 'removed', 'changed'].flatMap(kind => Object.entries(diff[kind]).map(([name, value]) =>
      <section key={name} aria-label={`${kind[0].toUpperCase() + kind.slice(1)} ${name}`}>
        <h5>{kind[0].toUpperCase() + kind.slice(1)} {name}</h5>
        {kind === 'changed' ? <><p>Before</p><Values role={value.before} /><p>After</p><Values role={value.after} /></> : <Values role={value} />}
      </section>))}
  </section>
}
