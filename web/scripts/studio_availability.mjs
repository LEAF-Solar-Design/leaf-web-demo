#!/usr/bin/env node
// Prints the Studio availability manifest (src/site/studioAvailability.js) for
// a tool that is not the browser: the capability measure, an audit, a lane.
//
//   node scripts/studio_availability.mjs --json      the whole table as JSON
//   node scripts/studio_availability.mjs             one line per feature
//
// Read-only. The table is edited in the source file, in the pull request that
// lands (or takes away) the feature.
import { STUDIO_AVAILABILITY_SCHEMA, EVIDENCE_ROOTS, listStudioAvailability } from '../src/site/studioAvailability.js'

const features = listStudioAvailability().map(({ declared, ...feature }) => feature)
if (process.argv.includes('--json')) {
  const counts = { enabled: 0, unavailable: 0 }
  for (const feature of features) counts[feature.status] += 1
  process.stdout.write(`${JSON.stringify({ schema: STUDIO_AVAILABILITY_SCHEMA, evidence_roots: EVIDENCE_ROOTS, counts, features }, null, 2)}\n`)
} else {
  for (const feature of features) {
    const state = feature.available ? 'enabled    ' : 'unavailable'
    process.stdout.write(`${state}  ${feature.id.padEnd(38)}  ${feature.available ? feature.evidence : feature.disclaimer}\n`)
  }
}
