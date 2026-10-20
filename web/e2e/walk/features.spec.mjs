import { test, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { collectProbeCases, declareProbes } from './collection.mjs'

const cases = collectProbeCases(buildFeatureMap().entries, resolveProbe)
declareProbes(test, runProbe, cases.ordinary)
