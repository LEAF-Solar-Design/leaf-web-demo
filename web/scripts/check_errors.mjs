// M4 oracle: the humanizer must never leak transport plumbing onto the stage.
// Recomputed against the REAL module — nothing here is hardcoded to a fixture.
//
// Run: node scripts/check_errors.mjs   (from web/)
import { humanizeError } from '../src/errorHumanize.js'

let failures = 0
const fail = (msg) => { failures++; console.error('FAIL:', msg) }
const ok = (msg) => console.log('  ok -', msg)

// A leak is any HTTP verb, any status arrow / bare status, any /api/ path.
const LEAKS = [
  ['->', (s) => s.includes('->')],
  ['502', (s) => /\b\d{3}\b/.test(s)],
  ['HTTP verb', (s) => /\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b/.test(s)],
  ['/api/', (s) => s.includes('/api/')],
]

function assertNoLeak(label, input) {
  const out = humanizeError(input)
  if (typeof out !== 'string' || !out.trim()) { fail(`${label}: empty output`); return null }
  for (const [name, hit] of LEAKS) {
    if (hit(out)) { fail(`${label}: output leaks ${name} -> ${JSON.stringify(out)}`); return out }
  }
  ok(`${label} -> ${JSON.stringify(out)}`)
  return out
}

// (1) The exact case named in the spec.
assertNoLeak("Error('POST /api/run -> 502')", new Error('POST /api/run -> 502'))

// (2) The other transport shapes that reach the DOM today.
assertNoLeak("Error('GET /api/session?dwg=... -> 500')", new Error('GET /api/session?dwg=abc.dwg -> 500'))
assertNoLeak("'lost job x: GET ... -> 502'", 'lost job 91f2c0aa: GET /api/jobs/91f2c0aa -> 502')
assertNoLeak("Error('POST /api/author -> 403')", new Error('POST /api/author -> 403'))
assertNoLeak("Error('DELETE /api/drawings/d1/checkout -> 409')", new Error('DELETE /api/drawings/d1/checkout -> 409'))

// (3) Network / unreachable service -> a calm service-unavailable message.
for (const [label, input] of [
  ["'Failed to fetch'", 'Failed to fetch'],
  ['TypeError(Failed to fetch)', Object.assign(new TypeError('Failed to fetch'), {})],
  ["'NetworkError when attempting to fetch resource.'", 'NetworkError when attempting to fetch resource.'],
]) {
  const out = assertNoLeak(label, input)
  if (out === null) continue
  const calm = /couldn't reach|could not reach|unavailable|connection/i.test(out)
  if (!calm) fail(`${label}: not a calm service-unavailable message -> ${JSON.stringify(out)}`)
  if (/fetch|networkerror|typeerror/i.test(out)) fail(`${label}: raw transport wording survived -> ${JSON.stringify(out)}`)
}

// (4) Already plain-English messages pass through UNCHANGED.
for (const plain of ['ops role required', 'not the lock holder', 'This drawing is checked out by someone else.']) {
  const out = humanizeError(plain)
  if (out !== plain) fail(`plain-English message was rewritten: ${JSON.stringify(plain)} -> ${JSON.stringify(out)}`)
  else ok(`passthrough ${JSON.stringify(plain)}`)
}

// (5) Junk / empty falls back to a generic calm sentence (never blank, never raw).
for (const junk of [null, undefined, '', {}, new Error('')]) {
  const out = humanizeError(junk)
  if (typeof out !== 'string' || out.trim().length < 5) fail(`no generic fallback for ${JSON.stringify(String(junk))}`)
}
ok('generic fallback for empty/unknown input')

// (6) Purity: the module must be import.meta / React free (node ran it, but be
// explicit so a future edit can't sneak a bundler-only dependency in).
const src = await (await import('node:fs/promises')).readFile(new URL('../src/errorHumanize.js', import.meta.url), 'utf8')
if (/import\.meta/.test(src)) fail('errorHumanize.js references import.meta')
if (/from ['"]react/.test(src)) fail('errorHumanize.js imports React')
if (/\.json['"]/.test(src)) fail('errorHumanize.js imports JSON')

if (failures) {
  console.error(`\n${failures} check(s) failed`)
  process.exit(1)
}
console.log('ERRORS_OK')
