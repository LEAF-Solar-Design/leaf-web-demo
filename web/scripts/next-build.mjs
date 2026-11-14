import { spawnSync } from 'node:child_process'
import { existsSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { bundleManifest, sourceIdentity } from './next-runtime.mjs'
import { distDigest, recordPrebuilt, reusablePrebuilt } from './ci-prebuilt.mjs'

const require = createRequire(import.meta.url)
const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const { loadEnvConfig } = require('@next/env')
loadEnvConfig(webRoot, false)
// Keep the exact same source identity in DefinePlugin and the export manifest.
process.env.LEAF_SOURCE_SHA = sourceIdentity(webRoot)
const reuseCiPrebuilt = process.argv.slice(2).includes('--reuse-ci-prebuilt')
const buildArgs = process.argv.slice(2).filter(arg => arg !== '--reuse-ci-prebuilt')
const prebuiltOptions = {
  webRoot, receiptPath: process.env.LEAF_WEB_PREBUILT_RECEIPT,
  sourceSha: process.env.LEAF_SOURCE_SHA, env: process.env, args: buildArgs,
}
if (reuseCiPrebuilt) {
  const reusable = reusablePrebuilt(prebuiltOptions)
  if (reusable.ok) {
    console.log(`web-build: reused CI prerequisite bundle source=${process.env.LEAF_SOURCE_SHA} dist=${distDigest(path.join(webRoot, 'dist')).slice(0, 12)}`)
    process.exit(0)
  }
  console.log(`web-build: building (prebuilt not reusable: ${reusable.reason})`)
}
// Dev copies must never become production inputs. Production engine staging
// remains the separate scripts/stage_cad_engine.mjs deployment step.
for (const name of ['engine.js', 'engine_bg.wasm']) rmSync(path.join(webRoot, 'public/engine', name), { force: true })
rmSync(path.join(webRoot, 'out'), { recursive: true, force: true })
const result = spawnSync(process.execPath, [require.resolve('next/dist/bin/next'), 'build', '--webpack', ...buildArgs], {
  cwd: webRoot, stdio: 'inherit', env: process.env,
})
if (result.error) throw result.error
if (result.status !== 0) process.exit(result.status ?? 1)

const outDir = path.join(webRoot, 'out')
if (!existsSync(path.join(outDir, 'index.html'))) throw new Error('Next did not emit out/index.html')
const manifest = bundleManifest(outDir, process.env.LEAF_SOURCE_SHA)
writeFileSync(path.join(outDir, 'web-build-manifest.json'), JSON.stringify(manifest, null, 2) + '\n')
rmSync(path.join(webRoot, 'dist'), { recursive: true, force: true })
renameSync(outDir, path.join(webRoot, 'dist'))
if (process.env.LEAF_WEB_PREBUILT_RECEIPT) {
  try {
    recordPrebuilt(prebuiltOptions)
  } catch (error) {
    console.warn(`web-build: warning: could not record CI prerequisite bundle: ${error.message}`)
  }
}
