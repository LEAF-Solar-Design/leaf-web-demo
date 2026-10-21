import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

const dockerfile = readFileSync(process.argv[2] ?? '../deploy/Dockerfile.web', 'utf8')
const workflow = readFileSync('../.github/workflows/build-platform-images.yml', 'utf8')
const index = readFileSync('index.html', 'utf8')
const api = readFileSync('src/api.js', 'utf8')
const intake = readFileSync('src/site/intakeCache.js', 'utf8')

assert.doesNotMatch(index, /https:\/\/fonts\.googleapis\.com/,
  'the deployable page must not fetch an external font stylesheet')
assert.match(api, /const API_BASE = import\.meta\.env\??\.VITE_API_BASE \?\? ''/,
  'the browser API must default to the current origin')
assert.match(intake, /const API_BASE = import\.meta\.env\??\.VITE_API_BASE \?\? ''/,
  'stage startup requests must default to the current origin')
assert.match(dockerfile, /^ARG VITE_API_BASE=$/m,
  'the deployable web image must default to same-origin API requests')
assert.match(dockerfile, /^ARG VITE_AUTH0_DOMAIN=leafautomation\.us\.auth0\.com$/m)
assert.match(dockerfile, /^ARG VITE_AUTH0_CLIENT_ID=zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O$/m)
assert.match(dockerfile, /^ARG VITE_AUTH0_AUDIENCE=https:\/\/api\.leafdesign\.ai$/m)
assert.match(dockerfile, /^ARG VITE_LIFECYCLE_UI=1$/m,
  'the deployable web image must bake the reviewed lifecycle UI selector on')
// cad_edit flipped ON 2026-08-30 by operator direction (leaf-web-demo #823).
// One shared web image serves both staging and production, so this pin is the
// documented all-environments enable; rollback = 0 here AND in the Dockerfile
// AND both build-workflow env blocks together.
assert.match(dockerfile, /^ARG VITE_CAD_EDIT=1$/m,
  'the deployable web image must bake the operator-enabled CAD editing surface on')
assert.match(dockerfile, /VITE_AUTH0_DOMAIN=\$\{VITE_AUTH0_DOMAIN\}/)
assert.match(dockerfile, /VITE_AUTH0_CLIENT_ID=\$\{VITE_AUTH0_CLIENT_ID\}/)
assert.match(dockerfile, /VITE_AUTH0_AUDIENCE=\$\{VITE_AUTH0_AUDIENCE\}/)
assert.match(dockerfile, /VITE_LIFECYCLE_UI=\$\{VITE_LIFECYCLE_UI\}/)
// The ARG is inert unless the ENV block forwards it into the vite build.
assert.match(dockerfile, /VITE_CAD_EDIT=\$\{VITE_CAD_EDIT\}/,
  'the VITE_CAD_EDIT build ARG must reach the vite build via the ENV block')
assert.match(dockerfile, /^ARG VITE_SOLAR_FLOW_RAIL=1$/m,
  'the VITE_SOLAR_FLOW_RAIL build ARG must exist and enable the shipped Solar flow rail')
assert.match(dockerfile, /^ARG VITE_SOLAR_SETTINGS_FORM=1$/m,
  'the VITE_SOLAR_SETTINGS_FORM build ARG must exist and enable the shipped Solar settings form')
assert.match(dockerfile, /VITE_SOLAR_FLOW_RAIL=\$\{VITE_SOLAR_FLOW_RAIL\}/,
  'the VITE_SOLAR_FLOW_RAIL build ARG must reach the vite build via the ENV block')
assert.match(dockerfile, /VITE_SOLAR_SETTINGS_FORM=\$\{VITE_SOLAR_SETTINGS_FORM\}/,
  'the VITE_SOLAR_SETTINGS_FORM build ARG must reach the vite build via the ENV block')
assert.equal((workflow.match(/VITE_SOLAR_FLOW_RAIL: "1"/g) || []).length, 2,
  'both web-build env blocks must enable VITE_SOLAR_FLOW_RAIL')
assert.equal((workflow.match(/VITE_SOLAR_SETTINGS_FORM: "1"/g) || []).length, 2,
  'both web-build env blocks must enable VITE_SOLAR_SETTINGS_FORM')

// Integration origin must agree across Docker, reuse fingerprints, and the
// prebuilt static artifacts. Scope each check to its producer role.
const webBuildStage = dockerfile.match(/^FROM node:22-slim AS build\n([\s\S]*?)(?=^FROM )/m)?.[1]
assert.ok(webBuildStage, 'the web Docker build stage must exist')
assert.match(webBuildStage, /^ARG VITE_INTEGRATIONS_API_ORIGIN=$/m,
  'an unset integration origin must stay disabled by default')
const viteEnv = webBuildStage.match(/^ENV VITE_API_BASE=[^\n]*(?:\n[^\n]*)*?(?=\n\n)/m)?.[0]
assert.ok(viteEnv, 'the Vite build ENV block must exist')
assert.match(viteEnv, /^    VITE_INTEGRATIONS_API_ORIGIN=\$\{VITE_INTEGRATIONS_API_ORIGIN\}(?: \\)?$/m,
  'the integration origin ARG must reach Vite through the build ENV')
assert.match(webBuildStage, /JSON\.stringify\(\{schema:"leaf\.web-build-config\.v1",vite_cad_edit:process\.env\.VITE_CAD_EDIT,vite_integrations_api_origin:process\.env\.VITE_INTEGRATIONS_API_ORIGIN\}/,
  'the image must safely attest the exact origin and CAD value from environment data')

const warmJob = workflow.match(/^  warm:\n([\s\S]*?)(?=^  [\w-]+:)/m)?.[1]
assert.ok(warmJob, 'the layer cache warming job must exist')
const warmSteps = warmJob.split(/^      - name: /m).slice(1)
const workflowSteps = workflow.replace(warmJob, '').split(/^      - name: /m).slice(1)
function stepById(id, steps = workflowSteps) {
  const matches = steps.filter(step => step.split('\n').includes(`        id: ${id}`))
  assert.equal(matches.length, 1, `exactly one ${id} step must exist`)
  return matches[0]
}
function stepEnv(step) {
  const env = step.match(/^        env:\n((?:^          [^\n]*\n)+)/m)?.[1]
  assert.ok(env, 'the producer step must have an env block')
  return env
}
assert.doesNotMatch(workflow, /vars\.VITE_INTEGRATIONS_API_ORIGIN/,
  'integration origin must come only from reviewed source configuration')
const warmOrigin = stepById('surface', warmSteps)
assert.match(warmOrigin, /^        if: steps\.chain\.outputs\.skip != 'true' && matrix\.image == 'web'$/m)
assert.ok(warmOrigin.includes('VITE_INTEGRATIONS_API_ORIGIN="$(python scripts/ci/native_release_producer.py web-origin)"'))
assert.ok(warmOrigin.includes('echo "integration_origin=$VITE_INTEGRATIONS_API_ORIGIN" >> "$GITHUB_OUTPUT"'))
assert.ok(warmSteps.indexOf(warmOrigin) < warmSteps.indexOf(stepById('warm-image', warmSteps)),
  'cache warming must validate the same source origin before building')
for (const [id, steps, surface] of [
  ['build-image', workflowSteps, 'surface'],
  ['spec-build-image', workflowSteps, 'spec-surface'],
  ['warm-image', warmSteps, 'surface'],
]) {
  const step = stepById(id, steps)
  assert.match(step, /^        uses: docker\/build-push-action@v6$/m)
  assert.match(step, /^          file: deploy\/Dockerfile\.\$\{\{ matrix\.image \}\}$/m)
  const args = step.match(/^          build-args: \|\n((?:^            [^\n]*\n)+)/m)?.[1]
  assert.ok(args, `${id} must pass Docker build arguments`)
  assert.ok(args.split('\n').includes("            ${{ matrix.image == 'web' && format('VITE_INTEGRATIONS_API_ORIGIN={0}', steps." + surface + ".outputs.integration_origin) || '' }}"),
    `${id} must pass the source integration origin only to web`)
}
for (const id of ['surface', 'spec-surface']) {
  const step = stepById(id)
  const run = step.split('        run: |\n')[1]
  assert.ok(run, `${id} must calculate a surface fingerprint`)
  assert.ok(run.includes('VITE_INTEGRATIONS_API_ORIGIN="$(python scripts/ci/native_release_producer.py web-origin)"'),
    `${id} must validate reviewed source before fingerprint or reuse`)
  assert.ok(run.includes('echo "integration_origin=$VITE_INTEGRATIONS_API_ORIGIN" >> "$GITHUB_OUTPUT"'),
    `${id} must publish the validated source origin`)
  assert.ok(run.indexOf('native_release_producer.py web-origin') < run.indexOf('case "${{ matrix.image }}"'),
    `${id} must resolve the origin before any reuse exits`)
  const webArgs = run.split('if [ "${{ matrix.image }}" = "web" ]; then').slice(1)
    .map(block => block.split('\n          fi')[0])
    .find(block => block.includes('build_args+=('))
  assert.ok(webArgs?.includes('build_args+=(--build-arg "VITE_INTEGRATIONS_API_ORIGIN=$VITE_INTEGRATIONS_API_ORIGIN")'),
    `${id} must fingerprint the same integration origin build argument for web`)
  const fingerprint = run.match(/python scripts\/platform_release_manifest\.py surface-fingerprint \\\n(?:[^\n]*\\\n)*[^\n]*/)?.[0]
  assert.ok(fingerprint?.includes('"${build_args[@]}"'),
    `${id} must pass the web build arguments into the fingerprint calculation`)
}
for (const id of ['surface-web', 'spec-web']) {
  const surface = id === 'surface-web' ? 'surface' : 'spec-surface'
  const step = stepById(id)
  assert.match(step, /^        working-directory: web$/m)
  assert.match(step, /^        if: matrix\.image == 'web' &&/m)
  assert.ok(stepEnv(step).split('\n').includes('          VITE_INTEGRATIONS_API_ORIGIN: ${{ steps.' + surface + '.outputs.integration_origin }}'),
    `${id} must bake the same source integration origin into the static artifact`)
  assert.match(step, /^          npm run build$/m)
}

// W7 (docs/convergence/ACCEPTANCE.md, Version 3): the old shell and its
// runtime rail are deleted, so the app always renders the studio shell. The
// image must not install the flags entrypoint drop-in, and the page must not
// load a flags file: nothing at boot may select a shell. (The patterns match
// the separator loosely so this file names no deleted path verbatim.)
assert.doesNotMatch(dockerfile, /write.runtime.flags/,
  'the web image must not install the deleted runtime flags entrypoint drop-in')
assert.doesNotMatch(dockerfile, /VITE_ONE_SHELL/,
  'no build-time VITE fence may select a shell either')
assert.doesNotMatch(index, /\/runtime.flags\.js/,
  'index.html must not load a runtime flags file')
assert.match(index, /<script type="module" src="\/src\/main\.jsx">/,
  'index.html must still load the bundle')

// Match the filesystem layout supplied to Vite by the build stage, including
// relative imports made by the vendored files reached from web/src.
const buildStart = /^FROM\s+\S+\s+AS\s+build\s*$/im.exec(dockerfile)
const buildTail = buildStart ? dockerfile.slice(buildStart.index + buildStart[0].length) : ''
const nextStage = /^FROM /im.exec(buildTail)
const buildStage = (nextStage ? buildTail.slice(0, nextStage.index) : buildTail)
  .replace(/\\\r?\n/g, ' ')
const stripTrailingSlashes = value => value.replace(/\/+$/, '')
const copies = [...buildStage.matchAll(/^\s*COPY\s+([^\r\n]+)/gim)].map(match => {
  const tokens = match[1].trim().split(/\s+/)
  const operands = tokens.filter(token => !token.startsWith('--'))
  return {
    stage: tokens.some(token => token.startsWith('--from=')),
    source: stripTrailingSlashes(operands.at(-2)),
    destination: stripTrailingSlashes(operands.at(-1)),
  }
})
const containsPath = (parent, candidate) => candidate === parent || candidate.startsWith(parent + '/')
const stageProvides = repoPath => copies.some(copy =>
  copy.stage && containsPath(copy.destination, '/' + repoPath))
const contextProvides = repoPath => copies.some(copy =>
  !copy.stage && copy.destination === '/' + copy.source && containsPath(copy.source, repoPath))
const repoRoot = path.resolve('..')
const repoPathOf = file => path.relative(repoRoot, file).split(path.sep).join('/')
const queue = []
function collectWebSources(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name)
    if (entry.isDirectory() && entry.name !== '__tests__') collectWebSources(file)
    else if (entry.isFile() && /\.(js|jsx|mjs|ts|tsx)$/.test(entry.name)
      && !/\.(test|spec)\.[^.]+$/.test(entry.name)) {
      queue.push({ file, importer: repoPathOf(file), specifier: './' + entry.name })
    }
  }
}
collectWebSources(path.join(repoRoot, 'web/src'))
const suffixes = ['', '.ts', '.tsx', '.js', '.jsx', '.mjs',
  '/index.ts', '/index.tsx', '/index.js', '/index.jsx', '/index.mjs']
const specifierPatterns = [
  /\bfrom\s*(['"])([^'"\r\n]+)\1/g,
  /\bimport\s*(['"])([^'"\r\n]+)\1/g,
  /\bimport\s*\(\s*(['"])([^'"\r\n]+)\1\s*\)/g,
  /\bnew\s+URL\s*\(\s*(['"])([^'"\r\n]+)\1\s*,\s*import\.meta\.url\s*\)/g,
]
const missing = []
const walked = new Set()
let walkLimitExceeded = false
const describeFailure = (repoPath, importer, specifier) =>
  `${repoPath} (imported by ${importer} as '${specifier}')`
for (let cursor = 0; cursor < queue.length; cursor += 1) {
  const { file, importer, specifier } = queue[cursor]
  if (walked.has(file)) continue
  if (walked.size >= 5000) {
    walkLimitExceeded = true
    missing.push(describeFailure(repoPathOf(file), importer, specifier))
    break
  }
  walked.add(file)
  const source = readFileSync(file, 'utf8')
  for (const pattern of specifierPatterns) {
    for (const match of source.matchAll(pattern)) {
      const dependency = match[2]
      if (!dependency.startsWith('./') && !dependency.startsWith('../')) continue
      const resolved = path.resolve(path.dirname(file), dependency)
      const repoPath = repoPathOf(resolved)
      if (containsPath('web', repoPath) || stageProvides(repoPath)) continue
      if (!contextProvides(repoPath)) {
        missing.push(describeFailure(repoPath, repoPathOf(file), dependency))
        continue
      }
      const target = suffixes.map(suffix => resolved + suffix)
        .find(candidate => existsSync(candidate) && statSync(candidate).isFile())
      if (!target) missing.push(describeFailure(repoPath, repoPathOf(file), dependency))
      else queue.push({ file: target, importer: repoPathOf(file), specifier: dependency })
    }
  }
}
assert.equal(missing.length, 0,
  `build stage is missing imports${walkLimitExceeded ? ' (walk exceeds 5000 files)' : ''}: ${missing.join('; ')}`)

console.log(`web container config: page assets, API, integration origin producers and fingerprints, Auth0 SPA values, Solar flow flags, and the absence of a shell rail are pinned; the build stage provides every out-of-tree import; checked ${walked.size} files`)
